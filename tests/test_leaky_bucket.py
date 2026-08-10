from app.algorithms.leaky_bucket import LeakyBucketRateLimiter


def test_allows_requests_up_to_capacity():
    limiter = LeakyBucketRateLimiter(
        capacity=5,
        leak_rate=1
    )

    for _ in range(5):
        assert limiter.allow_request() is True

    assert limiter.allow_request() is False


def test_remaining_requests():
    limiter = LeakyBucketRateLimiter(
        capacity=5,
        leak_rate=1
    )

    limiter.allow_request()
    limiter.allow_request()

    assert limiter.remaining_requests() == 3