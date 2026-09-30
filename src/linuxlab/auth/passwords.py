"""Password hashing with Argon2id.

Hashing and verification take tens of milliseconds of CPU and 64 MiB of memory
each. They run in worker threads so the event loop keeps serving terminals, and a
semaphore caps how many run at once.
"""

import asyncio
import secrets

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError

MIN_PASSWORD_LENGTH = 12
MAX_PASSWORD_LENGTH = 128

# Concurrent Argon2 operations. Each one uses 64 MiB with the default parameters.
MAX_CONCURRENT_HASHES = 2


def password_length_ok(password: str) -> bool:
    return MIN_PASSWORD_LENGTH <= len(password) <= MAX_PASSWORD_LENGTH


class Passwords:
    def __init__(
        self, hasher: PasswordHasher | None = None, *, concurrency: int = MAX_CONCURRENT_HASHES
    ) -> None:
        # argon2-cffi defaults to Argon2id with the RFC 9106 low-memory parameters.
        self._hasher = hasher or PasswordHasher()
        self._slots = asyncio.Semaphore(concurrency)
        # Computed once at startup, so the first unknown-email login costs the same
        # as any other.
        self._dummy_hash = self._hasher.hash(secrets.token_urlsafe(32))

    async def hash(self, password: str) -> str:
        async with self._slots:
            return await asyncio.to_thread(self._hasher.hash, password)

    async def verify(self, password_hash: str, password: str) -> bool:
        async with self._slots:
            return await asyncio.to_thread(self._verify, password_hash, password)

    async def verify_missing_user(self, password: str) -> None:
        """Spend the same work as a real verification when no account matches.

        Keeps the response time of a login for an unknown email close to one with
        a wrong password.
        """
        await self.verify(self._dummy_hash, password)

    def _verify(self, password_hash: str, password: str) -> bool:
        try:
            return self._hasher.verify(password_hash, password)
        except (VerificationError, InvalidHashError):
            return False
