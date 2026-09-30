"""In-memory rate limiting for sign-up and login.

Counts attempts per key (endpoint and peer address) in a sliding window. State
lives in the API process, which matches the single-process deployment of the
closed beta; it is lost on restart and not shared between processes.
"""

import time
from collections import deque
from collections.abc import Callable

ATTEMPTS = 5
WINDOW_SECONDS = 15 * 60

# Past this many tracked keys, keys with no recent attempts are dropped.
PRUNE_THRESHOLD = 10_000


class RateLimiter:
    def __init__(
        self,
        attempts: int = ATTEMPTS,
        window_seconds: float = WINDOW_SECONDS,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._attempts = attempts
        self._window = window_seconds
        self._clock = clock
        self._hits: dict[str, deque[float]] = {}

    def hit(self, key: str) -> float | None:
        """Record an attempt. Returns None if allowed, or seconds to wait if not.

        A refused attempt is not recorded, so the wait never grows while blocked.
        """
        now = self._clock()
        hits = self._hits.get(key)
        if hits is None:
            if len(self._hits) >= PRUNE_THRESHOLD:
                self._prune(now)
            hits = self._hits[key] = deque()
        self._expire(hits, now)
        if len(hits) >= self._attempts:
            return hits[0] + self._window - now
        hits.append(now)
        return None

    def _expire(self, hits: deque[float], now: float) -> None:
        while hits and hits[0] <= now - self._window:
            hits.popleft()

    def _prune(self, now: float) -> None:
        for key in list(self._hits):
            self._expire(self._hits[key], now)
            if not self._hits[key]:
                del self._hits[key]
