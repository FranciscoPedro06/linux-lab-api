"""Sign-up, login, logout and the current user. See docs/api.md#authentication."""

import hmac
import logging
import math
import uuid
from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import Depends, Request, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from linuxlab.api import ApiError, api_router
from linuxlab.auth import sessions
from linuxlab.auth.accounts import normalize_email, valid_display_name, valid_email
from linuxlab.auth.cookies import (
    SESSION_COOKIE,
    clear_session_cookie,
    clear_session_cookie_header,
    set_session_cookie,
)
from linuxlab.auth.models import User
from linuxlab.auth.passwords import (
    MAX_PASSWORD_LENGTH,
    MIN_PASSWORD_LENGTH,
    Passwords,
    password_length_ok,
)
from linuxlab.auth.ratelimit import RateLimiter
from linuxlab.auth.tokens import MAX_TOKEN_LENGTH
from linuxlab.config import Settings
from linuxlab.labs.lifecycle import Labs
from linuxlab.labs.models import EndReason

logger = logging.getLogger(__name__)

router = api_router(prefix="/api/auth")

UNIQUE_VIOLATION = "23505"

# Upper bounds on raw input, far above any valid value. Longer fields are rejected
# as a malformed request before any other check.
MAX_INPUT_LENGTH = 1024


class SignupRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: str = Field(max_length=MAX_INPUT_LENGTH)
    password: str = Field(max_length=MAX_INPUT_LENGTH)
    display_name: str = Field(max_length=MAX_INPUT_LENGTH)
    invite_code: str = Field(max_length=MAX_INPUT_LENGTH)


class LoginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: str = Field(max_length=MAX_INPUT_LENGTH)
    password: str = Field(max_length=MAX_INPUT_LENGTH)


class PublicUser(BaseModel):
    id: uuid.UUID
    email: str
    display_name: str

    @classmethod
    def of(cls, user: User) -> "PublicUser":
        return cls(id=user.id, email=user.email, display_name=user.display_name)


async def database(request: Request) -> AsyncIterator[AsyncSession]:
    sessionmaker: async_sessionmaker[AsyncSession] = request.app.state.sessionmaker
    async with sessionmaker() as db:
        yield db


Database = Annotated[AsyncSession, Depends(database)]


def _not_authenticated() -> ApiError:
    return ApiError(
        401,
        "not_authenticated",
        "Sessão inválida ou expirada. Entre novamente.",
        headers=clear_session_cookie_header(),
    )


async def current_user(request: Request, db: Database) -> User:
    """The user of the request's session. Raises 401 without a valid session."""
    token = request.cookies.get(SESSION_COOKIE)
    if not token or len(token) > MAX_TOKEN_LENGTH:
        raise _not_authenticated()
    active = await sessions.resolve(db, token, sessions.utcnow())
    if active is None:
        raise _not_authenticated()
    return active.user


CurrentUser = Annotated[User, Depends(current_user)]


def _check_rate(request: Request, endpoint: str) -> None:
    """Counts the attempt against the peer address. Runs before any Argon2 work."""
    limiter: RateLimiter = request.app.state.auth_rate_limits[endpoint]
    # The connection's peer, not X-Forwarded-For: there is no trusted proxy yet.
    peer = request.client.host if request.client else "unknown"
    retry_after = limiter.hit(f"{endpoint}:{peer}")
    if retry_after is not None:
        logger.info("rate limited: endpoint=%s", endpoint)
        raise ApiError(
            429,
            "rate_limited",
            "Muitas tentativas. Aguarde alguns minutos e tente novamente.",
            headers={"retry-after": str(max(1, math.ceil(retry_after)))},
        )


def _check_invite(settings: Settings, invite_code: str) -> None:
    expected = settings.signup_invite_code
    if expected is None:
        raise ApiError(403, "signup_disabled", "O cadastro está fechado no momento.")
    if not hmac.compare_digest(
        invite_code.strip().encode(), expected.get_secret_value().strip().encode()
    ):
        raise ApiError(403, "invalid_invite_code", "Código de convite inválido.")


@router.post("/signup", status_code=201)
async def signup(
    body: SignupRequest, request: Request, response: Response, db: Database
) -> PublicUser:
    _check_rate(request, "signup")
    _check_invite(request.app.state.settings, body.invite_code)

    email = valid_email(body.email)
    if email is None:
        raise ApiError(422, "invalid_email", "Informe um email válido.")
    if not password_length_ok(body.password):
        raise ApiError(
            422,
            "invalid_password",
            f"A senha deve ter entre {MIN_PASSWORD_LENGTH} e {MAX_PASSWORD_LENGTH} caracteres.",
        )
    display_name = valid_display_name(body.display_name)
    if display_name is None:
        raise ApiError(
            422, "invalid_display_name", "O nome deve ter entre 1 e 80 caracteres, sem controles."
        )

    passwords: Passwords = request.app.state.passwords
    user = User(
        id=uuid.uuid4(),
        email=email,
        password_hash=await passwords.hash(body.password),
        display_name=display_name,
    )
    db.add(user)
    try:
        # The unique index on the email decides a concurrent sign-up; no prior SELECT.
        await db.flush()
        token = sessions.add_session(db, user.id, sessions.utcnow())
        await db.commit()
    except IntegrityError as error:
        await db.rollback()
        if getattr(error.orig, "sqlstate", None) == UNIQUE_VIOLATION:
            raise ApiError(409, "email_taken", "Já existe uma conta com este email.") from None
        raise

    logger.info("user signed up: user=%s", user.id)
    set_session_cookie(response, token)
    return PublicUser.of(user)


@router.post("/login")
async def login(
    body: LoginRequest, request: Request, response: Response, db: Database
) -> PublicUser:
    _check_rate(request, "login")
    passwords: Passwords = request.app.state.passwords

    email = normalize_email(body.email)
    user = await db.scalar(select(User).where(func.lower(User.email) == email))
    if user is None:
        await passwords.verify_missing_user(body.password)
        verified = False
    else:
        verified = await passwords.verify(user.password_hash, body.password)
    if user is None or not verified:
        raise ApiError(401, "invalid_credentials", "Email ou senha incorretos.")

    token = sessions.add_session(db, user.id, sessions.utcnow())
    await db.commit()
    set_session_cookie(response, token)
    return PublicUser.of(user)


@router.post("/logout", status_code=204)
async def logout(request: Request, db: Database) -> Response:
    """Ends the user's active lab, then the request's session, if any.

    The lab goes first. If ending it fails, the session is kept and the client can
    retry, so the session is never gone while its lab still counts as ready. If only
    removing the container fails, the lab stays terminating, the reaper finishes it,
    and logout completes.
    """
    token = request.cookies.get(SESSION_COOKIE)
    if token and len(token) <= MAX_TOKEN_LENGTH:
        active = await sessions.resolve(db, token, sessions.utcnow())
        if active is not None:
            user_id = active.user.id
            # No transaction stays open while the lab is removed.
            await db.rollback()
            labs: Labs = request.app.state.labs
            await labs.end_active(user_id, EndReason.LOGOUT)
        await sessions.revoke(db, token)
    response = Response(status_code=204)
    clear_session_cookie(response)
    return response


@router.get("/me")
async def me(user: CurrentUser) -> PublicUser:
    return PublicUser.of(user)
