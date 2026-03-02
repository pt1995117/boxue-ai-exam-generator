from __future__ import annotations

import os
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Deque, Dict, Tuple

from authn import compute_canary_bucket


class SlidingWindowRateLimiter:
    def __init__(self, limit_per_minute: int = 120) -> None:
        self.limit_per_minute = max(limit_per_minute, 1)
        self._lock = threading.Lock()
        self._events: Dict[str, Deque[float]] = {}

    def allow(self, key: str) -> Tuple[bool, int]:
        now = time.time()
        window_start = now - 60.0
        with self._lock:
            dq = self._events.setdefault(key, deque())
            while dq and dq[0] < window_start:
                dq.popleft()
            if len(dq) >= self.limit_per_minute:
                retry_after = int(max(1, 60 - (now - dq[0])))
                return False, retry_after
            dq.append(now)
        return True, 0


@dataclass
class CircuitState:
    failures: int = 0
    opened_at: float = 0.0


class CircuitBreaker:
    def __init__(self, failure_threshold: int = 5, recovery_seconds: int = 30) -> None:
        self.failure_threshold = max(failure_threshold, 1)
        self.recovery_seconds = max(recovery_seconds, 1)
        self._lock = threading.Lock()
        self._states: Dict[str, CircuitState] = {}

    def allow(self, key: str) -> bool:
        now = time.time()
        with self._lock:
            st = self._states.setdefault(key, CircuitState())
            if st.opened_at <= 0:
                return True
            if now - st.opened_at >= self.recovery_seconds:
                st.opened_at = 0.0
                st.failures = 0
                return True
            return False

    def record_success(self, key: str) -> None:
        with self._lock:
            st = self._states.setdefault(key, CircuitState())
            st.failures = 0
            st.opened_at = 0.0

    def record_failure(self, key: str) -> None:
        now = time.time()
        with self._lock:
            st = self._states.setdefault(key, CircuitState())
            st.failures += 1
            if st.failures >= self.failure_threshold:
                st.opened_at = now


def select_release_channel(system_user: str, forced_channel: str = "") -> str:
    channel = forced_channel.strip().lower()
    if channel in {"stable", "canary"}:
        return channel
    percent = int(os.getenv("ADMIN_API_CANARY_PERCENT", "0"))
    salt = os.getenv("ADMIN_API_CANARY_SALT", "")
    bucket = compute_canary_bucket(system_user, salt=salt)
    return "canary" if bucket < max(0, min(100, percent)) else "stable"


rate_limiter = SlidingWindowRateLimiter(limit_per_minute=int(os.getenv("ADMIN_API_RATE_LIMIT_RPM", "240")))
circuit_breaker = CircuitBreaker(
    failure_threshold=int(os.getenv("ADMIN_API_CIRCUIT_FAILURES", "5")),
    recovery_seconds=int(os.getenv("ADMIN_API_CIRCUIT_RECOVERY_SEC", "30")),
)
