import time

from governance import CircuitBreaker, SlidingWindowRateLimiter, select_release_channel


def test_rate_limiter_blocks_after_limit():
    limiter = SlidingWindowRateLimiter(limit_per_minute=2)
    assert limiter.allow("u:/api")[0] is True
    assert limiter.allow("u:/api")[0] is True
    allowed, retry_after = limiter.allow("u:/api")
    assert allowed is False
    assert retry_after >= 1


def test_circuit_breaker_open_and_recover():
    breaker = CircuitBreaker(failure_threshold=2, recovery_seconds=1)
    assert breaker.allow("/api/hz/slices") is True
    breaker.record_failure("/api/hz/slices")
    assert breaker.allow("/api/hz/slices") is True
    breaker.record_failure("/api/hz/slices")
    assert breaker.allow("/api/hz/slices") is False
    time.sleep(1.1)
    assert breaker.allow("/api/hz/slices") is True


def test_release_channel_canary_selection(monkeypatch):
    monkeypatch.setenv("ADMIN_API_CANARY_PERCENT", "100")
    assert select_release_channel("any_user") == "canary"
    monkeypatch.setenv("ADMIN_API_CANARY_PERCENT", "0")
    assert select_release_channel("any_user") == "stable"
