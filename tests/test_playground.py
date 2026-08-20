import pytest
from fastapi.testclient import TestClient
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route

from app.main import app
from app.middleware.rate_limiter import RateLimiter
from app.middleware.rate_limit_middleware import RateLimitMiddleware

ALGORITHMS = [
    "fixed_window",
    "sliding_window",
    "token_bucket",
    "leaky_bucket",
]


@pytest.fixture
def client():
    return TestClient(app)


async def _echo(request):
    return JSONResponse({"ok": True})


def _mini_app():
    return Starlette(
        routes=[
            Route("/api/test", _echo),
            Route("/playground/echo", _echo),
            Route("/admin/x", _echo),
            Route("/admin2", _echo),
        ]
    )


def _limited(mini, excluded_prefixes):
    limiter = RateLimiter(limit=2, window=60)

    return TestClient(
        RateLimitMiddleware(
            mini,
            client_key_fn=lambda request: "unit-client",
            get_rate_limiter=lambda: limiter,
            excluded_prefixes=excluded_prefixes,
        )
    )


class TestConfigEndpoint:
    def test_config_returns_server_shape(self, client):
        response = client.get("/playground/api/config")

        assert response.status_code == 200
        data = response.json()

        assert data["version"]
        assert data["algorithm"] in ALGORITHMS
        assert data["backend"] in ("memory", "redis")
        assert isinstance(data["limit"], int)
        assert isinstance(data["window"], int)
        assert isinstance(data["route_limits"], dict)
        assert isinstance(data["redis"]["configured"], bool)
        assert isinstance(data["redis"]["available"], bool)

    def test_config_never_exposes_secrets(self, client):
        response = client.get("/playground/api/config")

        raw = response.text.lower()

        assert "password" not in raw
        assert "REDIS_URL" not in raw
        assert "admin_api_token" not in raw
        assert response.json()["redis"].get("url") is None


class TestStaticServed:
    def test_index_served(self, client):
        response = client.get("/playground/")

        assert response.status_code == 200
        assert "RateGuard" in response.text

    def test_assets_served(self, client):
        assert client.get("/playground/app.js").status_code == 200
        assert client.get("/playground/style.css").status_code == 200


class TestMiddlewareExclusion:
    def test_playground_prefix_never_limited(self):
        app = _limited(_mini_app(), ("/admin", "/playground"))

        for _ in range(10):
            response = app.get("/playground/echo")
            assert response.status_code == 200
            assert "X-RateLimit-Limit" not in response.headers

    def test_other_paths_still_limited(self):
        app = _limited(_mini_app(), ("/admin", "/playground"))

        app.get("/api/test")
        app.get("/api/test")
        assert app.get("/api/test").status_code == 429

    def test_default_prefix_only_admin(self):
        limiter = RateLimiter(limit=2, window=60)
        client = TestClient(
            RateLimitMiddleware(
                _mini_app(),
                client_key_fn=lambda request: "unit-client",
                get_rate_limiter=lambda: limiter,
            )
        )

        for _ in range(10):
            assert client.get("/admin/x").status_code == 200

        assert client.get("/admin2").status_code == 200
        assert client.get("/admin2").status_code == 200
        assert client.get("/admin2").status_code == 429

    def test_real_app_playground_not_rate_limited(self, client):
        for _ in range(15):
            response = client.get("/playground/api/config")
            assert response.status_code == 200
            assert "X-RateLimit-Limit" not in response.headers


class TestSimMemory:
    def _create(self, client, algorithm="fixed_window", limit=4, **extra):
        body = {
            "algorithm": algorithm,
            "limit": limit,
            "window": 60,
            "backend": "memory",
            "client_id": "test-client",
            "route": "/api/test",
        }
        body.update(extra)

        return client.post("/playground/sim/session", json=body)

    def test_create_returns_session(self, client):
        response = self._create(client)

        assert response.status_code == 201
        data = response.json()

        assert data["session_id"]
        assert data["state"]["algorithm"] == "fixed_window"
        assert data["state"]["limit"] == 4
        assert data["metrics"]["remaining"] == 4

    def test_request_returns_events_and_state(self, client):
        sid = self._create(client, limit=4).json()["session_id"]

        response = client.post(
            "/playground/sim/request",
            json={"session_id": sid, "count": 1},
        )

        assert response.status_code == 200
        data = response.json()

        assert len(data["events"]) == 1
        assert data["events"][0]["allowed"] is True
        assert data["events"][0]["status"] == 200
        assert data["state"]["remaining"] == 3
        assert data["metrics"]["allowed"] == 1

    @pytest.mark.parametrize("algorithm", ALGORITHMS)
    def test_memory_respects_limit_for_all_algorithms(self, client, algorithm):
        sid = self._create(client, algorithm=algorithm, limit=4).json()["session_id"]

        data = client.post(
            "/playground/sim/request",
            json={"session_id": sid, "count": 7},
        ).json()

        events = data["events"]
        allowed = sum(1 for e in events if e["allowed"])
        rejected = sum(1 for e in events if not e["allowed"])

        assert allowed == 4
        assert rejected == 3
        assert data["metrics"]["allowed"] == 4
        assert data["metrics"]["rejected"] == 3

    def test_reset_clears_state_and_metrics(self, client):
        sid = self._create(client, limit=4).json()["session_id"]

        client.post("/playground/sim/request", json={"session_id": sid, "count": 6})

        response = client.post(
            "/playground/sim/reset",
            json={"session_id": sid},
        )

        assert response.status_code == 200
        data = response.json()

        assert data["metrics"]["requests"] == 0
        assert data["metrics"]["allowed"] == 0
        assert data["state"]["used"] == 0
        assert data["state"]["remaining"] == 4

    def test_state_endpoint_does_not_consume(self, client):
        sid = self._create(client, limit=4).json()["session_id"]

        client.post("/playground/sim/request", json={"session_id": sid, "count": 2})

        first = client.get("/playground/sim/state", params={"session_id": sid}).json()
        second = client.get("/playground/sim/state", params={"session_id": sid}).json()

        assert first["metrics"]["requests"] == 2
        assert second["metrics"]["requests"] == 2
        assert first["state"]["remaining"] == second["state"]["remaining"]

    def test_unknown_session_404(self, client):
        response = client.post(
            "/playground/sim/request",
            json={"session_id": "does-not-exist", "count": 1},
        )

        assert response.status_code == 404

    def test_unknown_state_404(self, client):
        assert client.get(
            "/playground/sim/state",
            params={"session_id": "nope"},
        ).status_code == 404

    def test_invalid_algorithm_422(self, client):
        response = self._create(client, algorithm="bogus")
        assert response.status_code == 422

    def test_invalid_backend_422(self, client):
        response = self._create(client, backend="cloud")
        assert response.status_code == 422

    def test_invalid_count_422(self, client):
        sid = self._create(client).json()["session_id"]

        assert client.post(
            "/playground/sim/request",
            json={"session_id": sid, "count": 0},
        ).status_code == 422

        assert client.post(
            "/playground/sim/request",
            json={"session_id": sid, "count": 99999},
        ).status_code == 422

    def test_burst_events_carry_status_and_reset(self, client):
        sid = self._create(client, limit=2).json()["session_id"]

        data = client.post(
            "/playground/sim/request",
            json={"session_id": sid, "count": 5},
        ).json()

        statuses = [e["status"] for e in data["events"]]
        assert statuses == [200, 200, 429, 429, 429]
        assert all(e["remaining"] == 0 for e in data["events"][2:])
        assert all(isinstance(e["reset"], int) for e in data["events"])


class TestSimRedis:
    def _redis_available(self):
        try:
            from app.core.redis_client import get_redis
            return bool(get_redis().ping())
        except Exception:
            return False

    def test_redis_unavailable_never_falls_back(self, client, monkeypatch):
        monkeypatch.setattr(
            "app.playground.simulation.redis_available",
            lambda: False,
        )

        response = client.post(
            "/playground/sim/session",
            json={
                "algorithm": "token_bucket",
                "limit": 5,
                "window": 60,
                "backend": "redis",
            },
        )

        assert response.status_code == 503
        assert response.json()["detail"]["error"] == "Redis unavailable"

    def test_redis_session_works_and_cleans_keys(self, client):
        if not self._redis_available():
            pytest.skip("Redis is not available")

        import uuid

        from app.core.redis_client import get_redis

        response = client.post(
            "/playground/sim/session",
            json={
                "algorithm": "leaky_bucket",
                "limit": 5,
                "window": 60,
                "backend": "redis",
                "client_id": "pg-test-" + uuid.uuid4().hex[:8],
                "route": "/api/test",
            },
        )

        assert response.status_code == 201
        sid = response.json()["session_id"]

        data = client.post(
            "/playground/sim/request",
            json={"session_id": sid, "count": 12},
        ).json()

        allowed = sum(1 for e in data["events"] if e["allowed"])
        rejected = sum(1 for e in data["events"] if not e["allowed"])

        assert allowed == 5
        assert rejected == 7
        assert data["metrics"]["rejected"] == 7

        close_response = client.post(
            "/playground/sim/close",
            json={"session_id": sid},
        )
        assert close_response.status_code == 200

        redis = get_redis()
        leftovers = list(
            redis.scan_iter(match=f"rateguard:*playground:sim:{sid}*", count=200)
        )

        assert leftovers == []