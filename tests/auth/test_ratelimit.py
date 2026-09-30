import pytest

from linuxlab.auth import ratelimit
from linuxlab.auth.ratelimit import RateLimiter


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def test_allows_up_to_the_limit_then_refuses() -> None:
    clock = Clock()
    limiter = RateLimiter(5, 900, clock=clock)

    assert [limiter.hit("login:1.2.3.4") for _ in range(5)] == [None] * 5
    assert limiter.hit("login:1.2.3.4") == 900


def test_keys_are_independent() -> None:
    limiter = RateLimiter(1, 900, clock=Clock())

    assert limiter.hit("login:1.2.3.4") is None
    assert limiter.hit("login:5.6.7.8") is None
    assert limiter.hit("signup:1.2.3.4") is None
    assert limiter.hit("login:1.2.3.4") is not None


def test_window_slides() -> None:
    clock = Clock()
    limiter = RateLimiter(2, 900, clock=clock)
    limiter.hit("k")
    clock.now += 600
    limiter.hit("k")

    clock.now += 299
    assert limiter.hit("k") == 1

    clock.now += 1
    assert limiter.hit("k") is None
    assert limiter.hit("k") is not None


def test_refused_attempts_do_not_extend_the_wait() -> None:
    clock = Clock()
    limiter = RateLimiter(1, 900, clock=clock)
    limiter.hit("k")

    for _ in range(10):
        clock.now += 60
        limiter.hit("k")

    clock.now += 300
    assert limiter.hit("k") is None


def test_idle_keys_are_pruned(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ratelimit, "PRUNE_THRESHOLD", 3)
    clock = Clock()
    limiter = RateLimiter(5, 900, clock=clock)
    for key in ("a", "b", "c"):
        limiter.hit(key)

    clock.now += 901
    limiter.hit("d")

    assert set(limiter._hits) == {"d"}
