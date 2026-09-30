"""Server-side sessions.

A session is valid while both hold:
- less than SESSION_IDLE has passed since it was last seen;
- its absolute expiry, SESSION_LIFETIME after creation, has not passed.

Activity moves `last_seen_at` at most once every TOUCH_INTERVAL and never moves
`expires_at`.
"""

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from linuxlab.auth.models import AuthSession, User
from linuxlab.auth.tokens import new_token, token_hash

SESSION_IDLE = timedelta(days=7)
SESSION_LIFETIME = timedelta(days=30)
TOUCH_INTERVAL = timedelta(minutes=5)


def utcnow() -> datetime:
    return datetime.now(UTC)


def is_active(session: AuthSession, now: datetime) -> bool:
    return now < session.expires_at and now < session.last_seen_at + SESSION_IDLE


@dataclass(frozen=True)
class ActiveSession:
    session: AuthSession
    user: User


def add_session(db: AsyncSession, user_id: uuid.UUID, now: datetime) -> str:
    """Add a session to the transaction and return its token."""
    token = new_token()
    db.add(
        AuthSession(
            user_id=user_id,
            token_hash=token_hash(token),
            created_at=now,
            last_seen_at=now,
            expires_at=now + SESSION_LIFETIME,
        )
    )
    return token


async def resolve(db: AsyncSession, token: str, now: datetime) -> ActiveSession | None:
    row = (
        await db.execute(
            select(AuthSession, User)
            .join(User, User.id == AuthSession.user_id)
            .where(AuthSession.token_hash == token_hash(token))
        )
    ).one_or_none()
    if row is None:
        return None
    session, user = row
    if not is_active(session, now):
        await db.execute(delete(AuthSession).where(AuthSession.id == session.id))
        await db.commit()
        return None
    if now - session.last_seen_at >= TOUCH_INTERVAL:
        await db.execute(
            update(AuthSession).where(AuthSession.id == session.id).values(last_seen_at=now)
        )
        await db.commit()
        session.last_seen_at = now
    return ActiveSession(session, user)


async def revoke(db: AsyncSession, token: str) -> None:
    await db.execute(delete(AuthSession).where(AuthSession.token_hash == token_hash(token)))
    await db.commit()
