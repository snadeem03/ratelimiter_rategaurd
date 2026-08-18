import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest
import redis
from fastapi.testclient import TestClient

from app import main as app_module
from app.algorithms.factory import create_rate_limiter
from app.algorithms.redis_leaky_bucket import RedisLeakyBucketRateLimiter
from app.algorithms.redis_sliding_window import RedisSlidingWindowRateLimiter
from app.core.redis_client import get_redis
from app.middleware.rate_limiter import RateLimiter
from app.storage.redis_storage import RedisStorage


try:
    get_redis().ping()
except Exception:
    pytest.skip(
        "Redis is not available",
        allow_module_level=True
    )


def create_storage():
    return RedisStorage(get_redis())


def create_limiter(client_id, capacity=5, leak_rate=0.1, ttl=None):
    return RedisLeakyBucketRateLimiter(
        storage=create_storage(),
        client_id=client_id,
        capacity=capacity,
        leak_rate=leak_rate,
        ttl=ttl
    )


def cleanup(limiter):
    redis_client = limiter.redis_client

    for key in (
        limiter.key,
        limiter.seq_key,
        limiter.last_leak_key
    ):
        redis_client.delete(key)


def cleanup_keys(client_id, storage=None):
    storage = storage or create_storage()

    for found in storage.client.scan_iter(
        f"rateguard:leaky_bucket:{client_id}*"
    ):
        storage.client.delete(found)


def test_allows_requests_under_capacity():
    limiter = create_limiter(
        "lb-under-capacity",
        capacity=5,
        leak_rate=0.0001
    )

    try:
        for _ in range(3):
            assert limiter.allow_request() is True

        assert limiter.remaining_requests() == 2
    finally:
        cleanup(limiter)


def test_allows_requests_up_to_capacity():
    limiter = create_limiter(
        "lb-capacity",
        capacity=5,
        leak_rate=0.0001
    )

    try:
        for _ in range(5):
            assert limiter.allow_request() is True
    finally:
        cleanup(limiter)


def test_requests_at_capacity_remaining_zero():
    limiter = create_limiter(
        "lb-at-capacity",
        capacity=3,
        leak_rate=0.0001
    )

    try:
        for _ in range(3):
            assert limiter.allow_request() is True

        assert limiter.remaining_requests() == 0
    finally:
        cleanup(limiter)


def test_rejects_requests_over_capacity():
    limiter = create_limiter(
        "lb-over-capacity",
        capacity=2,
        leak_rate=0.0001
    )

    try:
        for _ in range(2):
            assert limiter.allow_request() is True

        assert limiter.allow_request() is False
        assert limiter.allow_request() is False
        assert limiter.remaining_requests() == 0
    finally:
        cleanup(limiter)


def test_remaining_never_negative():
    limiter = create_limiter(
        "lb-never-negative",
        capacity=3,
        leak_rate=0.0001
    )

    try:
        for _ in range(10):
            limiter.allow_request()
            assert limiter.remaining_requests() >= 0
    finally:
        cleanup(limiter)


def test_reset_time_zero_when_bucket_not_full():
    limiter = create_limiter(
        "lb-reset-zero",
        capacity=5,
        leak_rate=0.0001
    )

    try:
        assert limiter.reset_time() == 0

        limiter.allow_request()
        assert limiter.reset_time() == 0
    finally:
        cleanup(limiter)


def test_reset_time_after_block():
    limiter = create_limiter(
        "lb-reset",
        capacity=1,
        leak_rate=0.1
    )

    try:
        assert limiter.allow_request() is True
        assert limiter.allow_request() is False

        assert limiter.reset_time() > 0
    finally:
        cleanup(limiter)


def test_bucket_leaks_requests_over_time():
    limiter = create_limiter(
        "lb-leaks",
        capacity=5,
        leak_rate=10
    )

    try:
        for _ in range(5):
            assert limiter.allow_request() is True

        assert limiter.allow_request() is False

        time.sleep(0.25)

        assert limiter.allow_request() is True
    finally:
        cleanup(limiter)


def test_remaining_reflects_leaks():
    client_id = "lb-leak-remaining"

    a = create_limiter(
        client_id,
        capacity=5,
        leak_rate=10
    )

    try:
        for _ in range(5):
            assert a.allow_request() is True

        assert a.remaining_requests() == 0

        time.sleep(0.3)

        b = create_limiter(
            client_id,
            capacity=5,
            leak_rate=10
        )

        assert 0 < b.remaining_requests() < 5
    finally:
        cleanup(a)


@pytest.mark.parametrize("capacity,workers", [(1, 10), (5, 20)])
def test_concurrent_requests_only_capacity_allowed(capacity, workers):
    client_id = f"lb-concurrent-{capacity}"

    limiter = create_limiter(
        client_id,
        capacity=capacity,
        leak_rate=0.0001
    )

    barrier = threading.Barrier(workers)

    def fire(index):
        worker = create_limiter(
            client_id,
            capacity=capacity,
            leak_rate=0.0001
        )

        barrier.wait(timeout=10)

        return worker.allow_request()

    try:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            results = list(pool.map(fire, range(workers)))

        assert sum(results) == capacity
        assert sum(1 for r in results if not r) == workers - capacity
    finally:
        cleanup(limiter)


def test_client_keys_are_isolated():
    a = create_limiter(
        "lb-client-a",
        capacity=1,
        leak_rate=0.0001
    )
    b = create_limiter(
        "lb-client-b",
        capacity=1,
        leak_rate=0.0001
    )

    try:
        assert a.allow_request() is True
        assert b.allow_request() is True
        assert a.allow_request() is False
        assert b.allow_request() is False
    finally:
        cleanup(a)
        cleanup(b)


def test_route_isolation_via_facade():
    storage = create_storage()

    limiter = RateLimiter(
        limit=5,
        window=60,
        algorithm="leaky_bucket",
        storage=storage,
        route_limits={
            "/api/login": {"limit": 2, "window": 60},
            "/api/products": {"limit": 3, "window": 60},
        },
    )

    client = "route-iso-client"

    try:
        for _ in range(2):
            assert limiter.allow_request(
                client, route="/api/login"
            ) is True

        assert limiter.allow_request(
            client, route="/api/login"
        ) is False

        for _ in range(3):
            assert limiter.allow_request(
                client, route="/api/products"
            ) is True

        assert limiter.allow_request(
            client, route="/api/products"
        ) is False

        assert limiter.allow_request(
            client, route="/api/orders"
        ) is True
    finally:
        for found in storage.client.scan_iter(
            "rateguard:leaky_bucket:*route-iso-client*"
        ):
            storage.client.delete(found)


def test_algorithm_key_isolation():
    storage = create_storage()
    client_id = "algo-iso"

    lb = RedisLeakyBucketRateLimiter(
        storage=storage,
        client_id=client_id,
        capacity=1,
        leak_rate=0.0001
    )

    sw = RedisSlidingWindowRateLimiter(
        storage=storage,
        client_id=client_id,
        limit=1,
        window=60
    )

    try:
        assert lb.key != sw.key
        assert "leaky_bucket" in lb.key
        assert "sliding_window" in sw.key

        assert lb.allow_request() is True
        assert lb.allow_request() is False

        assert sw.allow_request() is True
        assert sw.allow_request() is False
    finally:
        for found in storage.client.scan_iter(
            f"rateguard:*:{client_id}*"
        ):
            storage.client.delete(found)


def test_shared_state_across_limiter_instances():
    client_id = "lb-shared-state"

    a = create_limiter(
        client_id,
        capacity=1,
        leak_rate=0.0001
    )
    b = create_limiter(
        client_id,
        capacity=1,
        leak_rate=0.0001
    )

    try:
        assert a.allow_request() is True
        assert b.allow_request() is False

        c = create_limiter(
            client_id,
            capacity=1,
            leak_rate=0.0001
        )

        assert c.remaining_requests() == 0
    finally:
        cleanup(a)


def test_ttl_set_on_state_keys():
    limiter = create_limiter(
        "lb-ttl",
        capacity=5,
        leak_rate=1,
        ttl=30
    )

    try:
        limiter.allow_request()

        assert limiter.redis_client.ttl(limiter.key) > 0
        assert limiter.redis_client.ttl(limiter.seq_key) > 0
        assert limiter.redis_client.ttl(limiter.last_leak_key) > 0
    finally:
        cleanup(limiter)


def test_default_ttl_matches_drain_time():
    limiter = create_limiter(
        "lb-ttl-default",
        capacity=5,
        leak_rate=0.1
    )

    try:
        assert limiter.ttl == 50
    finally:
        cleanup(limiter)


def test_factory_selects_redis_leaky_bucket():
    storage = create_storage()
    client_id = "lb-factory"

    limiter = create_rate_limiter(
        "leaky_bucket",
        storage=storage,
        limit=3,
        window=60,
        client_id=client_id
    )

    try:
        assert isinstance(limiter, RedisLeakyBucketRateLimiter)

        for _ in range(3):
            assert limiter.allow_request() is True

        assert limiter.allow_request() is False
    finally:
        cleanup(limiter)


def test_rate_limit_headers_via_facade():
    storage = create_storage()

    limiter = RateLimiter(
        limit=3,
        window=60,
        algorithm="leaky_bucket",
        storage=storage
    )

    key = "lb-headers"

    try:
        assert limiter.allow_request(key) is True

        headers = limiter.rate_limit_headers(key)
        assert headers["X-RateLimit-Limit"] == "3"
        assert 0 <= int(headers["X-RateLimit-Remaining"]) <= 2
        assert int(headers["X-RateLimit-Reset"]) >= 0

        limiter.allow_request(key)
        limiter.allow_request(key)

        assert limiter.allow_request(key) is False

        headers = limiter.rate_limit_headers(key)
        assert headers["X-RateLimit-Limit"] == "3"
        assert int(headers["X-RateLimit-Remaining"]) == 0
        assert int(headers["X-RateLimit-Reset"]) > 0
    finally:
        for found in storage.client.scan_iter(
            "rateguard:*lb-headers*"
        ):
            storage.client.delete(found)


@pytest.fixture
def client():
    return TestClient(app_module.app)


def test_http_429_retry_after_with_redis_leaky_bucket(client, monkeypatch):
    storage = create_storage()

    limiter = RateLimiter(
        limit=2,
        window=60,
        algorithm="leaky_bucket",
        storage=storage
    )

    monkeypatch.setattr(app_module, "rate_limiter", limiter)

    key = {"X-API-Key": "http-lb-redis"}

    try:
        for _ in range(2):
            response = client.get("/api/test", headers=key)
            assert response.status_code == 200
            assert response.headers["X-RateLimit-Limit"] == "2"

        response = client.get("/api/test", headers=key)

        assert response.status_code == 429
        assert response.headers["X-RateLimit-Limit"] == "2"
        assert int(response.headers["X-RateLimit-Remaining"]) == 0
        assert int(response.headers["X-RateLimit-Reset"]) > 0
        assert (
            response.headers["Retry-After"]
            == response.headers["X-RateLimit-Reset"]
        )
        assert (
            response.headers["Retry-After"]
            == str(response.json()["detail"]["retry_after"])
        )
    finally:
        for found in storage.client.scan_iter(
            "rateguard:*http-lb-redis*"
        ):
            storage.client.delete(found)


def test_redis_unavailable_fails_closed():
    class BrokenScript:
        def __call__(self, keys=None, args=None):
            raise redis.exceptions.ConnectionError("redis is down")

    class BrokenClient:
        def register_script(self, script):
            return BrokenScript()

    class BrokenStorage:
        client = BrokenClient()

    limiter = RedisLeakyBucketRateLimiter(
        storage=BrokenStorage(),
        client_id="lb-down",
        capacity=5,
        leak_rate=1
    )

    with pytest.raises(redis.exceptions.ConnectionError):
        limiter.allow_request()
