import pytest

from app.algorithms.factory import create_rate_limiter
from app.algorithms.fixed_window import FixedWindowRateLimiter
from app.algorithms.sliding_window import SlidingWindowRateLimiter
from app.algorithms.token_bucket import TokenBucketRateLimiter
from app.algorithms.leaky_bucket import LeakyBucketRateLimiter


def test_fixed_window_factory():
    limiter = create_rate_limiter("fixed_window")

    assert isinstance(
        limiter,
        FixedWindowRateLimiter
    )


def test_sliding_window_factory():
    limiter = create_rate_limiter("sliding_window")

    assert isinstance(
        limiter,
        SlidingWindowRateLimiter
    )


def test_token_bucket_factory():
    limiter = create_rate_limiter("token_bucket")

    assert isinstance(
        limiter,
        TokenBucketRateLimiter
    )


def test_leaky_bucket_factory():
    limiter = create_rate_limiter("leaky_bucket")

    assert isinstance(
        limiter,
        LeakyBucketRateLimiter
    )


def test_invalid_algorithm():
    with pytest.raises(ValueError):
        create_rate_limiter("something_random")