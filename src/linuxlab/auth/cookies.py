"""The session cookie. The token reaches the client only through it."""

from fastapi import Response

from linuxlab.auth.sessions import SESSION_LIFETIME

# The __Host- prefix makes browsers require Secure and Path=/ and refuse Domain,
# so the cookie is bound to exactly this origin.
SESSION_COOKIE = "__Host-sid"


def set_session_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=int(SESSION_LIFETIME.total_seconds()),
        path="/",
        secure=True,
        httponly=True,
        samesite="lax",
    )


def clear_session_cookie(response: Response) -> None:
    response.delete_cookie(SESSION_COOKIE, path="/", secure=True, httponly=True, samesite="lax")


def clear_session_cookie_header() -> dict[str, str]:
    response = Response()
    clear_session_cookie(response)
    return {"set-cookie": response.headers["set-cookie"]}
