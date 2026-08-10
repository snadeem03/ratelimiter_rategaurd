import time

from app.algorithms.leaky_bucket import LeakyBucketRateLimiter


def test_bucket_leaks_requests():
    limiter = LeakyBucketRateLimiter(
        capacity=5,
        leak_rate=10
    )

    for _ in range(5):
        assert limiter.allow_request() is True

    assert limiter.allow_request() is False

    time.sleep(0.2)

    assert limiter.allow_request() is True