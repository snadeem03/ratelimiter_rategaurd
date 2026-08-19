import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from starlette.applications import Starlette
from starlette.responses import JSONResponse, StreamingResponse
from starlette.routing import Route

from app import main as app_module
from app.api_keys import ApiKeyStore
from app.main import app
from app.middleware.rate_limiter import RateLimiter
from app.middleware.rate_limit_middleware import RateLimitMiddleware

ALGORITHMS = [
    "fixed_window",
    "sliding_window",
    "token_bucket",
    "leaky_bucket",
]

EXCLUDED_PATHS = {"/", "/docs", "/redoc", "/openapi.json"}

ROUTES = {
    "/api/login": {"limit": 10, "window": 60},
    "/api/products": {"limit": 200, "window": 60},
}


async def _echo(request):
    return JSONResponse({"ok": True})


async def _stream(request):
    async def body():
        yield b"chunk-1-"
        yield b"chunk-2"

    return StreamingResponse(body())


def _mini_app():
    return Starlette(
        routes=[
            Route("/", _echo),
            Route("/api/test", _echo),
            Route("/api/login", _echo, methods=["POST"]),
            Route("/api/products", _echo),
            Route("/api/orders", _echo, methods=["POST"]),
            Route("/stream", _stream),
        ]
    )


def _make_limited_app(
    limiter,
    client_key_fn=lambda request: "unit-client",
    excluded=None,
    route_limits=None,
):
    return RateLimitMiddleware(
        _mini_app(),
        client_key_fn=client_key_fn,
        get_rate_limiter=lambda: limiter,
        excluded_paths=excluded or EXCLUDED_PATHS,
        route_limits=route_limits or {},
    )


class TestMiddlewareUnit:
    """Direct middleware tests against a minimal Starlette app."""

    def test_allows_requests_under_limit(self):
        limiter = RateLimiter(limit=5, window=60)
        client = TestClient(_make_limited_app(limiter))

        for _ in range(5):
            response = client.get("/api/test")
            assert response.status_code == 200

    def test_returns_429_over_limit(self):
        limiter = RateLimiter(limit=2, window=60)
        client = TestClient(_make_limited_app(limiter))

        client.get("/api/test")
        client.get("/api/test")

        response = client.get("/api/test")
        assert response.status_code == 429
        assert response.json()["detail"]["error"] == "Too many requests"
        assert response.json()["detail"]["retry_after"] == int(
            response.headers["X-RateLimit-Reset"]
        )

    def test_limit_header(self):
        limiter = RateLimiter(limit=7, window=60)
        client = TestClient(_make_limited_app(limiter))

        response = client.get("/api/test")
        assert response.headers["X-RateLimit-Limit"] == "7"

    def test_remaining_header_decreases(self):
        limiter = RateLimiter(limit=5, window=60)
        client = TestClient(_make_limited_app(limiter))

        assert client.get("/api/test").headers["X-RateLimit-Remaining"] == "4"
        assert client.get("/api/test").headers["X-RateLimit-Remaining"] == "3"

    def test_reset_header_present(self):
        limiter = RateLimiter(limit=5, window=60)
        client = TestClient(_make_limited_app(limiter))

        response = client.get("/api/test")
        assert int(response.headers["X-RateLimit-Reset"]) >= 0

    def test_retry_after_matches_reset_on_429(self):
        limiter = RateLimiter(limit=1, window=60)
        client = TestClient(_make_limited_app(limiter))

        client.get("/api/test")
        response = client.get("/api/test")

        assert response.status_code == 429
        assert response.headers["X-RateLimit-Remaining"] == "0"
        assert (
            response.headers["Retry-After"]
            == response.headers["X-RateLimit-Reset"]
        )
        assert (
            response.headers["Retry-After"]
            == str(response.json()["detail"]["retry_after"])
        )

    @pytest.mark.parametrize("algorithm", ALGORITHMS)
    def test_all_algorithms_enforce_limits(self, algorithm):
        limiter = RateLimiter(limit=3, window=60, algorithm=algorithm)
        client = TestClient(_make_limited_app(limiter))

        for _ in range(3):
            assert client.get("/api/test").status_code == 200

        response = client.get("/api/test")
        assert response.status_code == 429
        assert response.headers["X-RateLimit-Limit"] == "3"

    def test_per_route_limits(self):
        limiter = RateLimiter(limit=5, window=60, route_limits=ROUTES)
        client = TestClient(_make_limited_app(limiter))

        for _ in range(10):
            response = client.post("/api/login")
            assert response.status_code == 200
            assert response.headers["X-RateLimit-Limit"] == "10"

        response = client.post("/api/login")
        assert response.status_code == 429
        assert response.headers["X-RateLimit-Limit"] == "10"

    def test_routes_keep_independent_limits(self):
        limiter = RateLimiter(limit=5, window=60, route_limits=ROUTES)
        client = TestClient(_make_limited_app(limiter))

        for _ in range(10):
            client.post("/api/login")

        response = client.get("/api/products")
        assert response.status_code == 200
        assert response.headers["X-RateLimit-Limit"] == "200"

    def test_global_fallback_for_unconfigured_route(self):
        limiter = RateLimiter(limit=2, window=60, route_limits=ROUTES)
        client = TestClient(_make_limited_app(limiter))

        client.get("/api/test")
        client.get("/api/test")

        response = client.get("/api/test")
        assert response.status_code == 429
        assert response.headers["X-RateLimit-Limit"] == "2"

    def test_client_isolation(self):
        def identity(request):
            return f"unit-{request.headers.get('X-Client')}"

        limiter = RateLimiter(limit=1, window=60)
        client = TestClient(
            _make_limited_app(limiter, client_key_fn=identity)
        )

        assert client.get(
            "/api/test", headers={"X-Client": "a"}
        ).status_code == 200
        assert client.get(
            "/api/test", headers={"X-Client": "a"}
        ).status_code == 429
        assert client.get(
            "/api/test", headers={"X-Client": "b"}
        ).status_code == 200

    def test_excluded_paths_not_limited(self):
        limiter = RateLimiter(limit=1, window=60)
        client = TestClient(_make_limited_app(limiter))

        for path in ("/", "/docs", "/redoc", "/openapi.json"):
            for _ in range(5):
                response = client.get(path)
                assert response.status_code != 429
                assert "X-RateLimit-Limit" not in response.headers

    def test_admin_prefix_never_limited(self):
        limiter = RateLimiter(limit=1, window=60)
        client = TestClient(_make_limited_app(limiter))

        for _ in range(5):
            response = client.get("/admin/api-keys")
            assert response.status_code == 404
            assert "X-RateLimit-Limit" not in response.headers

        response = client.get("/api/test")
        assert response.status_code == 200

    def test_explicitly_configured_excluded_path_is_limited(self):
        limiter = RateLimiter(
            limit=1,
            window=60,
            route_limits={"/docs": {"limit": 1, "window": 60}},
        )
        client = TestClient(
            _make_limited_app(
                limiter,
                route_limits={"/docs": {"limit": 1, "window": 60}},
            )
        )

        client.get("/docs")
        response = client.get("/docs")

        assert response.status_code == 429

    def test_streaming_response_passes_through(self):
        limiter = RateLimiter(limit=5, window=60)
        client = TestClient(_make_limited_app(limiter))

        response = client.get("/stream")

        assert response.status_code == 200
        assert response.content == b"chunk-1-chunk-2"
        assert response.headers["X-RateLimit-Limit"] == "5"
        assert response.headers["X-RateLimit-Remaining"] == "4"

    def test_identity_failure_renders_401(self):
        def bad_identity(request):
            raise HTTPException(
                status_code=401,
                detail={"error": "Invalid or inactive API key"},
            )

        limiter = RateLimiter(limit=5, window=60)
        client = TestClient(
            _make_limited_app(limiter, client_key_fn=bad_identity)
        )

        response = client.get("/api/test")
        assert response.status_code == 401
        assert response.json() == {
            "detail": {"error": "Invalid or inactive API key"}
        }

    @pytest.mark.parametrize("algorithm", ALGORITHMS)
    def test_concurrent_requests_allow_exactly_limit(self, algorithm):
        limiter = RateLimiter(limit=5, window=60, algorithm=algorithm)
        client = TestClient(_make_limited_app(limiter))

        workers = 20
        barrier = threading.Barrier(workers)

        def fire(index):
            barrier.wait(timeout=10)
            return client.get("/api/test").status_code

        with ThreadPoolExecutor(max_workers=workers) as pool:
            codes = list(pool.map(fire, range(workers)))

        assert sum(1 for code in codes if code == 200) == 5
        assert sum(1 for code in codes if code == 429) == 15

    def test_multiple_sequential_requests(self):
        limiter = RateLimiter(limit=5, window=60)
        client = TestClient(_make_limited_app(limiter))

        statuses = [client.get("/api/test").status_code for _ in range(8)]
        assert statuses == [200, 200, 200, 200, 200, 429, 429, 429]


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture
def key_store(monkeypatch):
    store = ApiKeyStore()
    monkeypatch.setattr(app_module, "api_key_store", store)
    return store


class TestMiddlewareIntegration:
    """Middleware behavior through the real RateGuard app."""

    def test_managed_api_key_becomes_identity(self, client, key_store, monkeypatch):
        limiter = RateLimiter(limit=2, window=60)
        monkeypatch.setattr(app_module, "rate_limiter", limiter)

        key, _ = key_store.create(name="app", owner="mw-owner")
        headers = {"X-API-Key": key}

        for _ in range(2):
            assert client.get("/api/test", headers=headers).status_code == 200

        response = client.get("/api/test", headers=headers)
        assert response.status_code == 429

        other, _ = key_store.create(name="app", owner="mw-other")
        response = client.get("/api/test", headers={"X-API-Key": other})
        assert response.status_code == 200

    def test_ip_fallback_when_no_api_key(self, client, monkeypatch):
        limiter = RateLimiter(limit=2, window=60)
        monkeypatch.setattr(app_module, "rate_limiter", limiter)

        for _ in range(2):
            assert client.get("/api/test").status_code == 200

        response = client.get("/api/test")
        assert response.status_code == 429
        assert response.headers["X-RateLimit-Limit"] == "2"

    def test_invalid_api_key_returns_401(self, client, key_store):
        response = client.get(
            "/api/test",
            headers={"X-API-Key": "rg_live_does_not_exist"},
        )
        assert response.status_code == 401

    def test_disabled_api_key_returns_401(self, client, key_store):
        key, meta = key_store.create(name="app")
        key_store.revoke(meta["id"])

        response = client.get("/api/test", headers={"X-API-Key": key})
        assert response.status_code == 401

    def test_excluded_paths_have_no_headers(self, client):
        for path in ("/", "/docs", "/redoc", "/openapi.json"):
            response = client.get(path)
            assert response.status_code == 200
            assert "X-RateLimit-Limit" not in response.headers

    def test_admin_api_never_rate_limited(self, client, monkeypatch, key_store):
        monkeypatch.setattr(app_module, "ADMIN_API_TOKEN", "mw-admin-token")
        limiter = RateLimiter(limit=1, window=60)
        monkeypatch.setattr(app_module, "rate_limiter", limiter)

        response = client.get(
            "/admin/api-keys",
            headers={"X-Admin-Token": "mw-admin-token"},
        )
        assert response.status_code == 200
        assert "X-RateLimit-Limit" not in response.headers

        client.get("/api/test", headers={"X-API-Key": "mw-key"})

        response = client.get(
            "/admin/api-keys",
            headers={"X-Admin-Token": "mw-admin-token"},
        )
        assert response.status_code == 200
        assert "X-RateLimit-Limit" not in response.headers

    def test_429_body_and_headers(self, client, monkeypatch):
        limiter = RateLimiter(limit=2, window=60)
        monkeypatch.setattr(app_module, "rate_limiter", limiter)
        key = {"X-API-Key": "mw-429"}

        client.get("/api/test", headers=key)
        client.get("/api/test", headers=key)

        response = client.get("/api/test", headers=key)
        assert response.status_code == 429
        assert response.headers["X-RateLimit-Limit"] == "2"
        assert response.headers["X-RateLimit-Remaining"] == "0"
        assert int(response.headers["X-RateLimit-Reset"]) > 0
        assert (
            response.headers["Retry-After"]
            == response.headers["X-RateLimit-Reset"]
        )
        body = response.json()
        assert body["detail"]["error"] == "Too many requests"
        assert body["detail"]["retry_after"] == int(
            response.headers["X-RateLimit-Reset"]
        )

    def test_success_body_keeps_remaining(self, client, monkeypatch):
        limiter = RateLimiter(limit=5, window=60)
        monkeypatch.setattr(app_module, "rate_limiter", limiter)

        response = client.get(
            "/api/test", headers={"X-API-Key": "mw-body"}
        )
        assert response.status_code == 200
        assert response.json() == {
            "message": "Request successful",
            "remaining": 4,
        }


def _redis_storage():
    from app.core.redis_client import get_redis
    from app.storage.redis_storage import RedisStorage

    try:
        get_redis().ping()
    except Exception:
        pytest.skip("Redis is not available")

    return RedisStorage(get_redis())


class TestMiddlewareRedis:
    @pytest.mark.parametrize("algorithm", ALGORITHMS)
    def test_all_redis_algorithms_over_http(self, client, monkeypatch, algorithm):
        storage = _redis_storage()
        limiter = RateLimiter(
            limit=3,
            window=60,
            algorithm=algorithm,
            storage=storage,
        )
        monkeypatch.setattr(app_module, "rate_limiter", limiter)

        key = {"X-API-Key": f"mw-redis-{algorithm}"}

        try:
            for _ in range(3):
                response = client.get("/api/test", headers=key)
                assert response.status_code == 200
                assert response.headers["X-RateLimit-Limit"] == "3"

            response = client.get("/api/test", headers=key)
            assert response.status_code == 429
            assert response.headers["X-RateLimit-Limit"] == "3"
            assert response.headers["X-RateLimit-Remaining"] == "0"
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
            for found in storage.client.scan_iter("rateguard:*mw-redis*"):
                storage.client.delete(found)

    def test_redis_concurrent_requests(self, monkeypatch):
        storage = _redis_storage()
        limiter = RateLimiter(
            limit=5,
            window=60,
            algorithm="sliding_window",
            storage=storage,
        )
        monkeypatch.setattr(app_module, "rate_limiter", limiter)

        client = TestClient(app)
        key = {"X-API-Key": "mw-redis-concurrent"}
        workers = 20
        barrier = threading.Barrier(workers)

        def fire(index):
            barrier.wait(timeout=10)
            return client.get("/api/test", headers=key).status_code

        try:
            with ThreadPoolExecutor(max_workers=workers) as pool:
                codes = list(pool.map(fire, range(workers)))

            assert sum(1 for code in codes if code == 200) == 5
            assert sum(1 for code in codes if code == 429) == 15
        finally:
            for found in storage.client.scan_iter(
                "rateguard:*mw-redis-concurrent*"
            ):
                storage.client.delete(found)