import asyncio
import time
from typing import Literal

import pytest
from argon2 import PasswordHasher, Type

from linuxlab.auth.passwords import (
    MAX_PASSWORD_LENGTH,
    MIN_PASSWORD_LENGTH,
    Passwords,
    password_length_ok,
)


async def test_hash_is_argon2id_and_never_contains_the_password() -> None:
    passwords = Passwords()

    hashed = await passwords.hash("correct horse battery")

    assert hashed.startswith("$argon2id$")
    assert "correct horse battery" not in hashed
    assert await passwords.verify(hashed, "correct horse battery")


async def test_wrong_password_and_malformed_hash_do_not_verify() -> None:
    passwords = Passwords()
    hashed = await passwords.hash("correct horse battery")

    assert not await passwords.verify(hashed, "correct horse batterY")
    assert not await passwords.verify("not a hash", "correct horse battery")


async def test_missing_user_runs_a_verification() -> None:
    calls = []

    class CountingHasher(PasswordHasher):
        def verify(self, hash: str | bytes, password: str | bytes) -> Literal[True]:
            calls.append(hash)
            return super().verify(hash, password)

    passwords = Passwords(CountingHasher())

    await passwords.verify_missing_user("whatever password")

    assert len(calls) == 1


@pytest.mark.parametrize(
    ("length", "accepted"),
    [
        (0, False),
        (MIN_PASSWORD_LENGTH - 1, False),
        (MIN_PASSWORD_LENGTH, True),
        (MAX_PASSWORD_LENGTH, True),
        (MAX_PASSWORD_LENGTH + 1, False),
    ],
)
def test_password_length_policy(length: int, accepted: bool) -> None:
    assert password_length_ok("a" * length) is accepted


def test_no_composition_rules() -> None:
    assert password_length_ok("aaaaaaaaaaaa")
    assert password_length_ok("só letras e espaços")


async def test_hashing_does_not_block_the_event_loop() -> None:
    # Deliberately slow parameters, so a blocking call would stall the ticker.
    passwords = Passwords(PasswordHasher(time_cost=8, memory_cost=65536, type=Type.ID))
    ticks = 0

    async def ticker() -> None:
        nonlocal ticks
        while True:
            await asyncio.sleep(0.01)
            ticks += 1

    task = asyncio.create_task(ticker())
    started = time.monotonic()
    await passwords.hash("correct horse battery")
    elapsed = time.monotonic() - started
    task.cancel()

    # The ticker kept running while the hash was computed.
    assert ticks >= max(2, int(elapsed / 0.01) // 4)


async def test_concurrent_operations_are_capped() -> None:
    running = 0
    peak = 0

    class ObservedHasher(PasswordHasher):
        def hash(self, password: str | bytes, *, salt: bytes | None = None) -> str:
            nonlocal running, peak
            running += 1
            peak = max(peak, running)
            try:
                time.sleep(0.05)
                return "$argon2id$fake"
            finally:
                running -= 1

    passwords = Passwords(ObservedHasher(), concurrency=2)

    await asyncio.gather(*(passwords.hash("password12345") for _ in range(6)))

    assert peak == 2
