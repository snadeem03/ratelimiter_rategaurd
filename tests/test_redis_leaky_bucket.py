import pytest

from app.core.redis_client import get_redis
from app.storage.redis_storage import RedisStorage

from app.algorithms.redis_leaky_bucket import (
    RedisLeakyBucketRateLimiter
)


try:
    get_redis().ping()
except Exception:
    pytest.skip(
        "Redis is not available",
        allow_module_level=True
    )


def create_limiter(client_id, capacity=5, leak_rate=0.1):
    storage = RedisStorage(
        get_redis()
    )

    limiter = RedisLeakyBucketRateLimiter(
        storage=storage,
        client_id=client_id,
        capacity=capacity,
        leak_rate=leak_rate
    )

    return limiter


def test_allows_requests_up_to_capacity():

    limiter = create_limiter("lb-capacity")

    try:
        for _ in range(5):
            assert limiter.allow_request() is True

        assert limiter.allow_request() is False
    finally:
        limiter.redis_client.delete(limiter.key)
        limiter.redis_client.delete(f"{limiter.key}:seq")


def test_remaining_requests():

    limiter = create_limiter("lb-remaining")

    try:
        limiter.allow_request()
        limiter.allow_request()

        assert limiter.remaining_requests() == 3
    finally:
        limiter.redis_client.delete(limiter.key)
        limiter.redis_client.delete(f"{limiter.key}:seq")


def test_reset_time_after_block():

    limiter = create_limiter("lb-reset", capacity=1)

    try:
        assert limiter.allow_request() is True
        assert limiter.allow_request() is False

        assert limiter.reset_time() > 0
    finally:
        limiter.redis_client.delete(limiter.key)
        limiter.redis_client.delete(f"{limiter.key}:seq")


def test_client_keys_are_isolated():

    a = create_limiter("lb-client-a", capacity=1)
    b = create_limiter("lb-client-b", capacity=1)

    try:
        assert a.allow_request() is True
        assert b.allow_request() is True
        assert a.allow_request() is False
        assert b.allow_request() is False
    finally:
        a.redis_client.delete(a.key)
        a.redis_client.delete(f"{a.key}:seq")
        b.redis_client.delete(b.key)
        b.redis_client.delete(f"{b.key}:seq")