import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.main import rate_limiter as app_rate_limiter
from app.middleware.rate_limiter import RateLimiter

ALGORITHMS = [
    "fixed_window",
    "sliding_window",
    "token_bucket",
    "leaky_bucket",
]


@pytest.fixture
def client():
    return TestClient(app)


def assert_limit_headers(
    response,
    limit,
    expected_remaining=None,
    reset_gte=0
):
    headers = response.headers
    assert headers["X-RateLimit-Limit"] == str(limit)

    remaining = int(headers["X-RateLimit-Remaining"])
    assert remaining >= 0

    if expected_remaining is not None:
        assert remaining == expected_remaining

    assert int(headers["X-RateLimit-Reset"]) >= reset_gte


class TestMemoryEndpoint:
    """HTTP-level tests against the app (memory backend)."""

    def test_success_response_has_headers(self, client):
        response = client.get(
            "/api/test",
            headers={"X-API-Key": "headers-success"}
        )
        assert response.status_code == 200
        assert_limit_headers(
            response,
            limit=app_rate_limiter.limit
        )
        assert (
            int(response.headers["X-RateLimit-Remaining"])
            <= app_rate_limiter.limit
        )

    def test_remaining_decreases(self, client):
        key = {"X-API-Key": "headers-remaining"}

        client.get("/api/test", headers=key)
        response = client.get("/api/test", headers=key)

        assert response.status_code == 200
        assert_limit_headers(
            response,
            limit=app_rate_limiter.limit,
            expected_remaining=app_rate_limiter.limit - 2
        )

    def test_429_response_has_headers_and_retry_after(self, client):
        key = {"X-API-Key": "headers-429"}

        for _ in range(app_rate_limiter.limit):
            client.get("/api/test", headers=key)

        response = client.get("/api/test", headers=key)

        assert response.status_code == 429
        assert_limit_headers(
            response,
            limit=app_rate_limiter.limit,
            expected_remaining=0,
            reset_gte=1
        )
        assert (
            response.headers["Retry-After"]
            == response.headers["X-RateLimit-Reset"]
        )
        assert (
            response.headers["Retry-After"]
            == str(response.json()["detail"]["retry_after"])
        )

    def test_remaining_never_negative(self, client):
        key = {"X-API-Key": "headers-never-negative"}

        for _ in range(app_rate_limiter.limit * 4):
            response = client.get("/api/test", headers=key)
            assert (
                int(response.headers["X-RateLimit-Remaining"]) >= 0
            )

    def test_client_isolation(self, client):
        key_a = {"X-API-Key": "headers-client-a"}
        key_b = {"X-API-Key": "headers-client-b"}

        for _ in range(app_rate_limiter.limit + 1):
            client.get("/api/test", headers=key_a)

        assert (
            client.get("/api/test", headers=key_a).status_code == 429
        )

        response = client.get("/api/test", headers=key_b)
        assert response.status_code == 200
        assert_limit_headers(
            response,
            limit=app_rate_limiter.limit,
            expected_remaining=app_rate_limiter.limit - 1
        )


@pytest.mark.parametrize("algorithm", ALGORITHMS)
class TestMemoryBackend:
    """Facade-level tests for all in-memory algorithms."""

    def test_headers_all_algorithms(self, algorithm):
        limiter = RateLimiter(limit=3, window=60, algorithm=algorithm)
        key = f"headers-memory-{algorithm}"

        assert limiter.allow_request(key) is True

        headers = limiter.rate_limit_headers(key)
        assert headers["X-RateLimit-Limit"] == "3"
        assert 0 <= int(headers["X-RateLimit-Remaining"]) <= 2
        assert int(headers["X-RateLimit-Reset"]) >= 0

        limiter.allow_request(key)
        limiter.allow_request(key)

        headers = limiter.rate_limit_headers(key)
        assert int(headers["X-RateLimit-Remaining"]) == 0

        assert limiter.allow_request(key) is False

        headers = limiter.rate_limit_headers(key)
        assert int(headers["X-RateLimit-Remaining"]) == 0
        assert int(headers["X-RateLimit-Reset"]) > 0


def redis_storage():
    from app.core.redis_client import get_redis
    from app.storage.redis_storage import RedisStorage

    try:
        get_redis().ping()
    except Exception:
        pytest.skip("Redis is not available")

    return RedisStorage(get_redis())


@pytest.mark.parametrize("algorithm", ALGORITHMS)
class TestRedisBackend:
    """Facade-level tests for all Redis-backed algorithms."""

    def test_headers_all_algorithms(self, algorithm):
        storage = redis_storage()
        limiter = RateLimiter(
            limit=3,
            window=60,
            algorithm=algorithm,
            storage=storage
        )
        key = f"headers-redis-{algorithm}"

        try:
            assert limiter.allow_request(key) is True

            headers = limiter.rate_limit_headers(key)
            assert headers["X-RateLimit-Limit"] == "3"
            assert 0 <= int(headers["X-RateLimit-Remaining"]) <= 2
            assert int(headers["X-RateLimit-Reset"]) >= 0

            limiter.allow_request(key)
            limiter.allow_request(key)

            headers = limiter.rate_limit_headers(key)
            assert int(headers["X-RateLimit-Remaining"]) == 0

            assert limiter.allow_request(key) is False

            headers = limiter.rate_limit_headers(key)
            assert int(headers["X-RateLimit-Remaining"]) == 0
            assert int(headers["X-RateLimit-Reset"]) > 0
        finally:
            for found in storage.client.scan_iter(
                "rateguard:*headers-redis*"
            ):
                storage.client.delete(found)