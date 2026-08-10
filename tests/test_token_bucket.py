from app.algorithms.token_bucket import TokenBucketRateLimiter


def test_allows_requests_up_to_capacity():
    limiter = TokenBucketRateLimiter(
        capacity=5,
        refill_rate=1
    )

    for _ in range(5):
        assert limiter.allow_request() is True

    assert limiter.allow_request() is False


def test_remaining_tokens():
    limiter = TokenBucketRateLimiter(
        capacity=5,
        refill_rate=1
    )

    limiter.allow_request()
    limiter.allow_request()

    assert limiter.remaining_requests() == 3