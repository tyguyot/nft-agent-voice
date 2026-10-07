"""
Simple in-memory rate limiter. Prevents accidental loops from blowing up API costs.
"""
import time
from collections import deque
from threading import Lock


class RateLimiter:
    def __init__(self, max_calls: int, window_seconds: int, name: str = "default"):
        self.max_calls = max_calls
        self.window_seconds = window_seconds
        self.name = name
        self._calls: deque = deque()
        self._lock = Lock()

    def check_and_record(self) -> tuple[bool, str]:
        now = time.time()
        cutoff = now - self.window_seconds

        with self._lock:
            while self._calls and self._calls[0] < cutoff:
                self._calls.popleft()

            if len(self._calls) >= self.max_calls:
                oldest = self._calls[0]
                retry_in = int(oldest + self.window_seconds - now)
                return (
                    False,
                    f"{self.name} rate limit hit ({self.max_calls}/{self.window_seconds}s). "
                    f"Retry in ~{retry_in}s.",
                )

            self._calls.append(now)
            return (True, f"{self.name}: {len(self._calls)}/{self.max_calls}")


draft_hourly = RateLimiter(max_calls=30, window_seconds=3600, name="draft_hourly")
draft_daily = RateLimiter(max_calls=200, window_seconds=86400, name="draft_daily")
reflect_daily = RateLimiter(max_calls=5, window_seconds=86400, name="reflect_daily")
