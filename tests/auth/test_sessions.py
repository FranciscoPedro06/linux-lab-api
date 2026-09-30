import uuid
from datetime import UTC, datetime, timedelta

from linuxlab.auth.models import AuthSession
from linuxlab.auth.sessions import SESSION_IDLE, SESSION_LIFETIME, is_active

CREATED = datetime(2026, 9, 1, tzinfo=UTC)


def session(last_seen: datetime) -> AuthSession:
    return AuthSession(
        user_id=uuid.uuid4(),
        token_hash=b"\0" * 32,
        created_at=CREATED,
        last_seen_at=last_seen,
        expires_at=CREATED + SESSION_LIFETIME,
    )


def test_active_until_idle_limit() -> None:
    seen = CREATED + timedelta(days=1)

    assert is_active(session(seen), seen + SESSION_IDLE - timedelta(seconds=1))
    assert not is_active(session(seen), seen + SESSION_IDLE)


def test_activity_does_not_extend_absolute_expiry() -> None:
    expiry = CREATED + SESSION_LIFETIME

    assert is_active(session(expiry - timedelta(minutes=1)), expiry - timedelta(seconds=1))
    assert not is_active(session(expiry - timedelta(minutes=1)), expiry)
