import time

from app.algorithms.token_bucket import TokenBucketRateLimiter


def test_tokens_refill():
    limiter = TokenBucketRateLimiter(
        capacity=5,
        refill_rate=10
    )

    # Consume all tokens
    for _ in range(5):
        assert limiter.allow_request() is True

    assert limiter.allow_request() is False

    # Wait for tokens to refill
    time.sleep(0.2)

    assert limiter.allow_request() is True