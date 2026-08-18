import pytest
from fastapi.testclient import TestClient

from app import main as app_module
from app.config import parse_route_limits
from app.main import app
from app.middleware.rate_limiter import RateLimiter

ROUTES = {
    "/api/login": {"limit": 10, "window": 60},
    "/api/products": {"limit": 200, "window": 60},
    "/api/orders": {"limit": 30, "window": 60},
}

ALGORITHMS = [
    "fixed_window",
    "sliding_window",
    "token_bucket",
    "leaky_bucket",
]


class TestParseRouteLimits:
    def test_parses_multiple_routes(self):
        raw = (
            "/api/login:10:60,"
            "/api/products:200:60,"
            "/api/orders:30:60"
        )

        assert parse_route_limits(raw) == ROUTES

    def test_empty_string(self):
        assert parse_route_limits("") == {}

    def test_none(self):
        assert parse_route_limits(None) == {}

    def test_ignores_whitespace(self):
        raw = (
            " /api/login : 10 : 60 , "
            "/api/products:200:60, /api/orders:30:60 "
        )
        assert parse_route_limits(raw) == ROUTES

    def test_invalid_entry_missing_parts(self):
        with pytest.raises(ValueError):
            parse_route_limits("/api/login:10")

    def test_invalid_entry_non_numeric(self):
        with pytest.raises(ValueError):
            parse_route_limits("/api/login:ten:60")

    def test_invalid_route_path(self):
        with pytest.raises(ValueError):
            parse_route_limits("api/login:10:60")


def _make_limiter(
    algorithm="sliding_window",
    limit=5,
    window=60,
    storage=None,
):
    return RateLimiter(
        limit=limit,
        window=window,
        algorithm=algorithm,
        storage=storage,
        route_limits=ROUTES,
    )


@pytest.mark.parametrize("algorithm", ALGORITHMS)
class TestMemoryRouteLimits:
    """Facade-level per-route tests for all in-memory algorithms."""

    def test_default_global_limit(self, algorithm):
        limiter = _make_limiter(algorithm)

        for _ in range(5):
            assert limiter.allow_request("client") is True

        assert limiter.allow_request("client") is False

    def test_route_specific_limit(self, algorithm):
        limiter = _make_limiter(algorithm)

        for _ in range(10):
            assert limiter.allow_request(
                "client", route="/api/login"
            ) is True

        assert limiter.allow_request(
            "client", route="/api/login"
        ) is False

    def test_multiple_routes_independent(self, algorithm):
        limiter = _make_limiter(algorithm)
        key = "client"

        for _ in range(10):
            assert limiter.allow_request(key, route="/api/login") is True

        assert limiter.allow_request(key, route="/api/orders") is True
        for _ in range(29):
            assert limiter.allow_request(
                key, route="/api/orders"
            ) is True
        assert limiter.allow_request(key, route="/api/orders") is False

        assert limiter.allow_request(key, route="/api/login") is False

    def test_multiple_clients_same_route(self, algorithm):
        limiter = _make_limiter(algorithm)

        for _ in range(10):
            limiter.allow_request("client-a", route="/api/login")

        assert limiter.allow_request(
            "client-a", route="/api/login"
        ) is False
        assert limiter.allow_request(
            "client-b", route="/api/login"
        ) is True

    def test_same_client_across_routes(self, algorithm):
        limiter = _make_limiter(algorithm)

        for _ in range(10):
            limiter.allow_request("client", route="/api/login")

        assert limiter.allow_request(
            "client", route="/api/products"
        ) is True

    def test_fallback_to_global_limit(self, algorithm):
        limiter = _make_limiter(algorithm)

        for _ in range(5):
            assert limiter.allow_request(
                "client", route="/api/unconfigured"
            ) is True

        assert limiter.allow_request(
            "client", route="/api/unconfigured"
        ) is False

    def test_headers_reflect_route_limit(self, algorithm):
        limiter = _make_limiter(algorithm)
        key = "client"

        limiter.allow_request(key, route="/api/login")

        headers = limiter.rate_limit_headers(key, route="/api/login")
        assert headers["X-RateLimit-Limit"] == "10"

    def test_headers_reflect_global_limit(self, algorithm):
        limiter = _make_limiter(algorithm)

        headers = limiter.rate_limit_headers("client")
        assert headers["X-RateLimit-Limit"] == "5"


@pytest.fixture
def client():
    return TestClient(app)


class TestHttpRouteLimits:
    """HTTP-level tests via the real endpoints (memory backend)."""

    def test_route_specific_limit_and_429(self, client, monkeypatch):
        limiter = _make_limiter()
        monkeypatch.setattr(app_module, "rate_limiter", limiter)

        key = {"X-API-Key": "http-login"}

        for _ in range(10):
            response = client.post("/api/login", headers=key)
            assert response.status_code == 200
            assert response.headers["X-RateLimit-Limit"] == "10"

        response = client.post("/api/login", headers=key)

        assert response.status_code == 429
        assert response.headers["X-RateLimit-Limit"] == "10"
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

    def test_routes_have_independent_limits(self, client, monkeypatch):
        limiter = _make_limiter()
        monkeypatch.setattr(app_module, "rate_limiter", limiter)

        key = {"X-API-Key": "http-independent"}

        for _ in range(10):
            assert client.post("/api/login", headers=key).status_code == 200

        response = client.get("/api/products", headers=key)
        assert response.status_code == 200
        assert response.headers["X-RateLimit-Limit"] == "200"
        assert int(response.headers["X-RateLimit-Remaining"]) == 199

    def test_client_isolation(self, client, monkeypatch):
        limiter = _make_limiter()
        monkeypatch.setattr(app_module, "rate_limiter", limiter)

        for _ in range(11):
            client.post("/api/login", headers={"X-API-Key": "iso-a"})

        assert (
            client.post(
                "/api/login", headers={"X-API-Key": "iso-a"}
            ).status_code
            == 429
        )

        response = client.post(
            "/api/login", headers={"X-API-Key": "iso-b"}
        )
        assert response.status_code == 200
        assert response.headers["X-RateLimit-Limit"] == "10"

    def test_global_fallback_on_unconfigured_route(self, client, monkeypatch):
        limiter = _make_limiter()
        monkeypatch.setattr(app_module, "rate_limiter", limiter)

        key = {"X-API-Key": "http-global"}

        for _ in range(5):
            assert client.get("/api/test", headers=key).status_code == 200

        response = client.get("/api/test", headers=key)
        assert response.status_code == 429
        assert response.headers["X-RateLimit-Limit"] == "5"


def redis_storage():
    from app.core.redis_client import get_redis
    from app.storage.redis_storage import RedisStorage

    try:
        get_redis().ping()
    except Exception:
        pytest.skip("Redis is not available")

    return RedisStorage(get_redis())


def _cleanup_redis(storage, marker="route-redis"):
    for found in storage.client.scan_iter(f"rateguard:*{marker}*"):
        storage.client.delete(found)


@pytest.mark.parametrize("algorithm", ALGORITHMS)
class TestRedisRouteLimits:
    """Facade-level per-route tests for all Redis-backed algorithms."""

    def test_route_specific_limit_and_headers(self, algorithm):
        storage = redis_storage()
        limiter = _make_limiter(algorithm, storage=storage)
        key = f"route-redis-{algorithm}"

        try:
            for _ in range(10):
                assert limiter.allow_request(
                    key, route="/api/login"
                ) is True

            assert limiter.allow_request(
                key, route="/api/login"
            ) is False

            headers = limiter.rate_limit_headers(key, route="/api/login")
            assert headers["X-RateLimit-Limit"] == "10"
            assert int(headers["X-RateLimit-Remaining"]) == 0
            assert int(headers["X-RateLimit-Reset"]) > 0
        finally:
            _cleanup_redis(storage, marker=f"route-redis-{algorithm}")

    def test_routes_independent(self, algorithm):
        storage = redis_storage()
        limiter = _make_limiter(algorithm, storage=storage)
        key = f"route-redis-{algorithm}"

        try:
            for _ in range(10):
                limiter.allow_request(key, route="/api/login")

            assert limiter.allow_request(
                key, route="/api/orders"
            ) is True

            headers = limiter.rate_limit_headers(key, route="/api/orders")
            assert headers["X-RateLimit-Limit"] == "30"

            assert limiter.allow_request(
                key, route="/api/login"
            ) is False
        finally:
            _cleanup_redis(storage, marker=f"route-redis-{algorithm}")

    def test_clients_isolated(self, algorithm):
        storage = redis_storage()
        limiter = _make_limiter(algorithm, storage=storage)
        key_a = f"route-redis-{algorithm}-a"
        key_b = f"route-redis-{algorithm}-b"

        try:
            for _ in range(10):
                limiter.allow_request(key_a, route="/api/login")

            assert limiter.allow_request(
                key_a, route="/api/login"
            ) is False
            assert limiter.allow_request(
                key_b, route="/api/login"
            ) is True
        finally:
            _cleanup_redis(storage, marker=f"route-redis-{algorithm}")

    def test_fallback_to_global_limit(self, algorithm):
        storage = redis_storage()
        limiter = _make_limiter(algorithm, storage=storage)
        key = f"route-redis-{algorithm}"

        try:
            for _ in range(5):
                assert limiter.allow_request(
                    key, route="/api/unconfigured"
                ) is True

            assert limiter.allow_request(
                key, route="/api/unconfigured"
            ) is False

            headers = limiter.rate_limit_headers(
                key, route="/api/unconfigured"
            )
            assert headers["X-RateLimit-Limit"] == "5"
        finally:
            _cleanup_redis(storage, marker=f"route-redis-{algorithm}")


class TestHttpRedisRouteLimits:
    """HTTP-level per-route tests via the real endpoints (Redis backend)."""

    def test_route_specific_limit_and_429(self, client, monkeypatch):
        storage = redis_storage()
        limiter = _make_limiter(storage=storage)
        monkeypatch.setattr(app_module, "rate_limiter", limiter)

        key = {"X-API-Key": "http-route-redis"}

        try:
            for _ in range(10):
                assert (
                    client.post("/api/login", headers=key).status_code
                    == 200
                )

            response = client.post("/api/login", headers=key)

            assert response.status_code == 429
            assert response.headers["X-RateLimit-Limit"] == "10"
            assert int(response.headers["X-RateLimit-Remaining"]) == 0
            assert int(response.headers["X-RateLimit-Reset"]) > 0
            assert (
                response.headers["Retry-After"]
                == response.headers["X-RateLimit-Reset"]
            )
        finally:
            _cleanup_redis(storage, marker="http-route-redis")
