from app.algorithms.sliding_window import SlidingWindowRateLimiter


def test_allows_requests_under_limit():
    limiter = SlidingWindowRateLimiter(
        limit=5,
        window=60
    )

    for _ in range(5):
        assert limiter.allow_request() is True


def test_blocks_requests_over_limit():
    limiter = SlidingWindowRateLimiter(
        limit=5,
        window=60
    )

    for _ in range(5):
        limiter.allow_request()

    assert limiter.allow_request() is False


def test_remaining_requests():
    limiter = SlidingWindowRateLimiter(
        limit=5,
        window=60
    )

    limiter.allow_request()
    limiter.allow_request()

    assert limiter.remaining_requests() == 3