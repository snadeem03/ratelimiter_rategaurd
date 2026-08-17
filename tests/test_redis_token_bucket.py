import pytest

from app.core.redis_client import get_redis
from app.storage.redis_storage import RedisStorage

from app.algorithms.redis_token_bucket import (
    RedisTokenBucketRateLimiter
)


try:
    get_redis().ping()
except Exception:
    pytest.skip(
        "Redis is not available",
        allow_module_level=True
    )


def create_limiter(client_id="test-user"):

    storage = RedisStorage(
        get_redis()
    )

    limiter = RedisTokenBucketRateLimiter(
        storage=storage,
        client_id=client_id,
        capacity=5,
        refill_rate=1
    )

    return limiter


def test_allows_requests_up_to_capacity():

    limiter = create_limiter(
        "capacity-test"
    )

    for _ in range(5):
        assert limiter.allow_request() is True

    assert limiter.allow_request() is False


def test_remaining_tokens():

    limiter = create_limiter(
        "remaining-test"
    )

    limiter.allow_request()
    limiter.allow_request()

    assert limiter.remaining_requests() == 3