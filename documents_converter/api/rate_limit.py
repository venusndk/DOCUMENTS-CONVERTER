"""
Basic in-memory, single-process rate limiting by client IP.

Deliberately simple: a fixed-window counter, not a sliding window or
token bucket -- good enough to blunt naive abuse of a single-process
deployment, which is what Phase 3 (docs/PHASE_0_AUDIT.md) actually built.
Does NOT share state across multiple worker processes or replicas; a real
multi-instance deployment needs a shared store (Redis, say) instead --
explicitly out of scope here, tracked as a known limitation rather than
quietly assumed away.
"""

from __future__ import annotations

import time
from threading import Lock


class FixedWindowRateLimiter:
    def __init__(self, max_requests: int, window_seconds: float):
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        self._counts: dict[str, tuple[int, float]] = {}
        self._lock = Lock()

    def allow(self, key: str) -> bool:
        """:return: True if this request should be allowed, False if `key`
        has exceeded max_requests within the current window."""
        now = time.monotonic()
        with self._lock:
            count, window_start = self._counts.get(key, (0, now))
            if now - window_start >= self.window_seconds:
                count, window_start = 0, now
            count += 1
            self._counts[key] = (count, window_start)
            return count <= self.max_requests
