import pytest

from app.algorithms.factory import create_rate_limiter
from app.algorithms.fixed_window import FixedWindowRateLimiter
from app.algorithms.sliding_window import SlidingWindowRateLimiter
from app.algorithms.token_bucket import TokenBucketRateLimiter
from app.algorithms.leaky_bucket import LeakyBucketRateLimiter
from app.algorithms.redis_fixed_window import RedisFixedWindowRateLimiter
from app.algorithms.redis_sliding_window import RedisSlidingWindowRateLimiter
from app.algorithms.redis_token_bucket import RedisTokenBucketRateLimiter
from app.algorithms.redis_leaky_bucket import RedisLeakyBucketRateLimiter


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


def redis_storage():
    from app.core.redis_client import get_redis
    from app.storage.redis_storage import RedisStorage

    try:
        get_redis().ping()
    except Exception:
        pytest.skip("Redis is not available")

    return RedisStorage(get_redis())


def test_redis_fixed_window_factory():
    limiter = create_rate_limiter(
        "fixed_window",
        storage=redis_storage(),
        client_id="factory-redis-fw"
    )

    assert isinstance(
        limiter,
        RedisFixedWindowRateLimiter
    )


def test_redis_sliding_window_factory():
    limiter = create_rate_limiter(
        "sliding_window",
        storage=redis_storage(),
        client_id="factory-redis-sw"
    )

    assert isinstance(
        limiter,
        RedisSlidingWindowRateLimiter
    )


def test_redis_token_bucket_factory():
    limiter = create_rate_limiter(
        "token_bucket",
        storage=redis_storage(),
        client_id="factory-redis-tb"
    )

    assert isinstance(
        limiter,
        RedisTokenBucketRateLimiter
    )


def test_redis_leaky_bucket_factory():
    limiter = create_rate_limiter(
        "leaky_bucket",
        storage=redis_storage(),
        client_id="factory-redis-lb"
    )

    assert isinstance(
        limiter,
        RedisLeakyBucketRateLimiter
    )


def test_redis_unsupported_algorithm():
    with pytest.raises(ValueError):
        create_rate_limiter(
            "something_random",
            storage=redis_storage()
        )