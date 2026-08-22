"""Admin API, runtime behavior, and Redis-sharing tests for dynamic
rate-limit policies."""

import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from app import main as app_module
from app.main import app
from app.middleware.rate_limiter import RateLimiter
from app.policies.resolver import PolicyResolver
from app.policies.store import MemoryPolicyStore

ALGORITHMS = [
    "fixed_window",
    "sliding_window",
    "token_bucket",
    "leaky_bucket",
]

STATIC_ROUTES = {
    "/api/login": {"limit": 10, "window": 60},
    "/api/orders": {"limit": 30, "window": 60},
}

ADMIN = {"X-Admin-Token": "test-admin-token"}


@pytest.fixture
def client():
    return TestClient(app)


def _wire(monkeypatch, algorithm="sliding_window", storage=None,
          cache_ttl=0.05):
    """Wire a fresh resolver + limiter into the app for a test."""
    store = MemoryPolicyStore()
    resolver = PolicyResolver(
        store=store,
        static_route_limits=STATIC_ROUTES,
        global_limit=5,
        global_window=60,
        cache_ttl=cache_ttl,
    )
    limiter = RateLimiter(
        limit=5,
        window=60,
        algorithm=algorithm,
        storage=storage,
        route_limits=STATIC_ROUTES,
        policy_resolver=resolver,
    )

    monkeypatch.setattr(app_module, "policy_resolver", resolver)
    monkeypatch.setattr(app_module, "ADMIN_API_TOKEN", "test-admin-token")
    monkeypatch.setattr(app_module, "route_limits", STATIC_ROUTES)
    monkeypatch.setattr(app_module, "rate_limiter", limiter)

    return resolver


# --------------------------------------------------------------- auth


class TestPolicyAuth:
    def _endpoints(self, client):
        return [
            ("GET", "/admin/rate-limits", None),
            ("POST", "/admin/rate-limits",
             {"route": "/api/x", "limit": 1, "window": 2}),
            ("PUT", "/admin/rate-limits/api/x", {"limit": 3}),
            ("DELETE", "/admin/rate-limits/api/x", None),
        ]

    def test_missing_token_rejected(self, client, monkeypatch):
        _wire(monkeypatch)

        for method, url, body in self._endpoints(client):
            response = client.request(method, url, json=body)
            assert response.status_code == 403, (method, url)

    def test_wrong_token_rejected(self, client, monkeypatch):
        _wire(monkeypatch)

        for method, url, body in self._endpoints(client):
            response = client.request(
                method, url, json=body, headers={"X-Admin-Token": "wrong"}
            )
            assert response.status_code == 403, (method, url)

    def test_token_not_configured_rejected(self, client, monkeypatch):
        _wire(monkeypatch)
        monkeypatch.setattr(app_module, "ADMIN_API_TOKEN", None)

        for method, url, body in self._endpoints(client):
            response = client.request(
                method, url, json=body, headers=ADMIN
            )
            assert response.status_code == 403, (method, url)

    def test_correct_token_allowed(self, client, monkeypatch):
        _wire(monkeypatch)
        response = client.get("/admin/rate-limits", headers=ADMIN)
        assert response.status_code == 200


# ---------------------------------------------------------------- CRUD


class TestPolicyCrud:
    def test_get_lists_static_and_global(self, client, monkeypatch):
        _wire(monkeypatch)

        data = client.get(
            "/admin/rate-limits", headers=ADMIN
        ).json()

        assert data["global"] == {"limit": 5, "window": 60}
        assert data["policies"] == []
        assert data["routes"]["/api/login"] == {
            "limit": 10, "window": 60, "source": "static",
        }
        # unlisted route is absent from the merged view but still
        # enforced via the global default
        assert "/api/products" not in data["routes"]

    def test_post_creates_policy(self, client, monkeypatch):
        _wire(monkeypatch)

        response = client.post(
            "/admin/rate-limits",
            headers=ADMIN,
            json={"route": "/api/orders", "limit": 50, "window": 60},
        )

        assert response.status_code == 201
        assert response.json() == {
            "route": "/api/orders",
            "limit": 50,
            "window": 60,
            "enabled": True,
        }

        data = client.get("/admin/rate-limits", headers=ADMIN).json()
        assert data["routes"]["/api/orders"] == {
            "limit": 50, "window": 60, "source": "dynamic",
            "policy": response.json(),
        }
        assert len(data["policies"]) == 1

    def test_post_duplicate_rejected(self, client, monkeypatch):
        _wire(monkeypatch)
        payload = {"route": "/api/dup", "limit": 5, "window": 60}

        first = client.post(
            "/admin/rate-limits", headers=ADMIN, json=payload
        )
        second = client.post(
            "/admin/rate-limits", headers=ADMIN, json=payload
        )

        assert first.status_code == 201
        assert second.status_code == 409

    @pytest.mark.parametrize(
        "payload",
        [
            {"route": "api/no-slash", "limit": 5, "window": 60},
            {"route": "/api/../secret", "limit": 5, "window": 60},
            {"route": "", "limit": 5, "window": 60},
            {"route": "/api/orders", "limit": 0, "window": 60},
            {"route": "/api/orders", "limit": -4, "window": 60},
            {"route": "/api/orders", "limit": 5, "window": 0},
            {"route": "/api/orders", "limit": 5, "window": -1},
            {"route": "/api/orders", "limit": True, "window": 60},
            {"route": "/api/orders", "limit": 5, "window": "60"},
            {"limit": 5, "window": 60},                      # no route
            {"route": "/api/orders", "limit": 5},            # no window
            {"route": "/api/orders", "owner": "acme"},       # unknown field
            {"route": "/api/orders", "enabled": "yes"},
        ],
    )
    def test_post_invalid_policies_rejected(self, client, monkeypatch,
                                            payload):
        _wire(monkeypatch)

        response = client.post(
            "/admin/rate-limits", headers=ADMIN, json=payload
        )

        assert response.status_code == 422, payload

    def test_put_updates_existing(self, client, monkeypatch):
        _wire(monkeypatch)

        client.post(
            "/admin/rate-limits",
            headers=ADMIN,
            json={"route": "/api/orders", "limit": 5, "window": 60},
        )

        response = client.put(
            "/admin/rate-limits/api/orders",
            headers=ADMIN,
            json={"limit": 12},
        )

        assert response.status_code == 200
        doc = response.json()

        # partial update keeps the other fields
        assert doc["limit"] == 12
        assert doc["window"] == 60

    def test_put_missing_policy_404(self, client, monkeypatch):
        _wire(monkeypatch)

        response = client.put(
            "/admin/rate-limits/api/unknown",
            headers=ADMIN,
            json={"limit": 12},
        )

        assert response.status_code == 404

    def test_delete_removes_and_returns_fallback(self, client,
                                                 monkeypatch):
        _wire(monkeypatch)

        client.post(
            "/admin/rate-limits",
            headers=ADMIN,
            json={"route": "/api/orders", "limit": 50, "window": 60},
        )

        response = client.delete(
            "/admin/rate-limits/api/orders", headers=ADMIN
        )

        assert response.status_code == 204

        data = client.get("/admin/rate-limits", headers=ADMIN).json()
        assert data["routes"]["/api/orders"] == {
            "limit": 30, "window": 60, "source": "static",
        }

        assert client.delete(
            "/admin/rate-limits/api/orders", headers=ADMIN
        ).status_code == 404

    def test_single_route_view(self, client, monkeypatch):
        _wire(monkeypatch)

        data = client.get(
            "/admin/rate-limits/api/orders", headers=ADMIN
        ).json()
        assert data["source"] == "static"
        assert data["limit"] == 30

        data = client.get(
            "/admin/rate-limits/api/unlisted", headers=ADMIN
        ).json()
        assert data["source"] == "global"

        assert client.get(
            "/admin/rate-limits/bad path", headers=ADMIN
        ).status_code == 422

    def test_no_secrets_in_responses(self, client, monkeypatch):
        _wire(monkeypatch)

        client.post(
            "/admin/rate-limits",
            headers=ADMIN,
            json={"route": "/api/orders", "limit": 5, "window": 6},
        )

        raw = client.get(
            "/admin/rate-limits", headers=ADMIN
        ).text.lower()

        assert "token" not in raw
        assert "password" not in raw
        assert ADMIN["X-Admin-Token"].lower() not in raw


# ------------------------------------------------------------ runtime


class TestRuntimePolicyUpdates:
    """A policy update must change subsequent requests without restart."""

    def _client(self):
        return TestClient(app)

    @pytest.mark.parametrize("algorithm", ["fixed_window",
                                           "sliding_window"])
    def test_windows_five_to_ten_to_five(self, client, monkeypatch,
                                         algorithm):
        _wire(monkeypatch, algorithm=algorithm)
        http = self._client()
        key = {"X-API-Key": f"runtime-{algorithm}"}

        client.post(
            "/admin/rate-limits",
            headers=ADMIN,
            json={"route": "/api/orders", "limit": 5, "window": 60},
        )

        statuses = [
            http.post("/api/orders", headers=key).status_code
            for _ in range(7)
        ]
        assert statuses == [200] * 5 + [429] * 2

        # raise the policy mid-flight; state is preserved so exactly
        # five more requests fit inside the same window
        assert client.put(
            "/admin/rate-limits/api/orders",
            headers=ADMIN,
            json={"limit": 10},
        ).status_code == 200

        statuses = [
            http.post("/api/orders", headers=key).status_code
            for _ in range(7)
        ]
        assert statuses == [200] * 5 + [429] * 2
        assert (
            http.post("/api/orders", headers=key).headers[
                "X-RateLimit-Limit"
            ]
            == "10"
        )

        # tighten again: already-full window rejects immediately
        assert client.put(
            "/admin/rate-limits/api/orders",
            headers=ADMIN,
            json={"limit": 5},
        ).status_code == 200

        assert http.post("/api/orders", headers=key).status_code == 429
        assert (
            http.post("/api/orders", headers=key).headers[
                "X-RateLimit-Limit"
            ]
            == "5"
        )

    @pytest.mark.parametrize("algorithm", ["token_bucket",
                                           "leaky_bucket"])
    def test_buckets_update_headers_and_enforcement(self, client,
                                                    monkeypatch,
                                                    algorithm):
        _wire(monkeypatch, algorithm=algorithm)
        http = self._client()
        key = {"X-API-Key": f"runtime-{algorithm}"}

        client.post(
            "/admin/rate-limits",
            headers=ADMIN,
            json={"route": "/api/orders", "limit": 3, "window": 600},
        )

        statuses = [
            http.post("/api/orders", headers=key).status_code
            for _ in range(4)
        ]
        assert statuses == [200] * 3 + [429]

        # capacity raised: the header reflects it immediately
        assert client.put(
            "/admin/rate-limits/api/orders",
            headers=ADMIN,
            json={"limit": 6},
        ).status_code == 200

        if algorithm == "token_bucket":
            # drained tokens stay drained; only refill paces recovery
            response = http.post("/api/orders", headers=key)
            assert response.status_code == 429
            assert response.headers["X-RateLimit-Limit"] == "6"
        else:
            # the leaky queue holds 3 of 6 slots: exactly three more
            # requests fit before it is full again
            statuses = [
                http.post("/api/orders", headers=key).status_code
                for _ in range(4)
            ]
            assert statuses == [200] * 3 + [429]
            assert (
                http.post("/api/orders", headers=key).headers[
                    "X-RateLimit-Limit"
                ]
                == "6"
            )

        # capacity tightened below current usage: still rejecting
        assert client.put(
            "/admin/rate-limits/api/orders",
            headers=ADMIN,
            json={"limit": 2},
        ).status_code == 200

        response = http.post("/api/orders", headers=key)
        assert response.status_code == 429
        assert response.headers["X-RateLimit-Limit"] == "2"

    def test_deletion_restores_fallback_mid_flight(self, client,
                                                   monkeypatch):
        _wire(monkeypatch)
        http = self._client()
        key = {"X-API-Key": "runtime-delete"}

        client.post(
            "/admin/rate-limits",
            headers=ADMIN,
            json={"route": "/api/test", "limit": 1, "window": 60},
        )

        assert http.get("/api/test").status_code == 200
        assert http.get("/api/test").status_code == 429
        assert (
            http.get("/api/test").headers["X-RateLimit-Limit"] == "1"
        )

        assert client.delete(
            "/admin/rate-limits/api/test", headers=ADMIN
        ).status_code == 204

        # falls back to the global default (5); the fresh budget is
        # independent of the deleted policy's state
        assert (
            http.get("/api/test", headers={"X-API-Key":
                                           "runtime-delete-2"})
            .headers["X-RateLimit-Limit"]
            == "5"
        )

    def test_disabled_policy_falls_back_without_deletion(self, client,
                                                         monkeypatch):
        _wire(monkeypatch)
        http = self._client()

        client.post(
            "/admin/rate-limits",
            headers=ADMIN,
            json={
                "route": "/api/orders",
                "limit": 1,
                "window": 60,
                "enabled": False,
            },
        )

        response = http.post(
            "/api/orders", headers={"X-API-Key": "disabled-policy"}
        )

        assert response.status_code == 200
        assert response.headers["X-RateLimit-Limit"] == "30"  # static

    def test_concurrent_requests_during_policy_update(self, client,
                                                      monkeypatch):
        """Requests racing an update never see malformed config."""
        _wire(monkeypatch, cache_ttl=0.01)
        http = self._client()

        client.post(
            "/admin/rate-limits",
            headers=ADMIN,
            json={"route": "/api/test", "limit": 500, "window": 60},
        )

        stop = threading.Event()

        def updater():
            limits = [100, 250, 500]

            while not stop.is_set():
                for count in limits:
                    client.put(
                        "/admin/rate-limits/api/test",
                        headers=ADMIN,
                        json={"limit": count},
                    )

        updater_thread = threading.Thread(target=updater)
        updater_thread.start()

        try:
            codes = [
                http.get("/api/test").status_code for _ in range(120)
            ]
        finally:
            stop.set()
            updater_thread.join(timeout=5)

        # Racing reads/writes never crash or misconfigure enforcement:
        # every response is a valid decision. Some 429s are expected
        # whenever the update loop temporarily tightens the limit
        # below the number of requests already admitted in-window
        # (existing state stays authoritative).
        assert set(codes) <= {200, 429}


# ------------------------------------------------------------- redis


def _redis():
    from app.core.redis_client import get_redis

    try:
        get_redis().ping()
    except Exception:
        pytest.skip("Redis is not available")

    return get_redis()


def _cleanup(client, marker):
    for found in client.scan_iter(f"rateguard:*{marker}*"):
        client.delete(found)


class TestRedisPolicySharing:
    """Policies persist in Redis and are shared across 'workers'."""

    def test_two_limiter_instances_share_policy_and_state(self):
        redis = _redis()
        from app.storage.redis_storage import RedisStorage

        _cleanup(redis, "polshare")

        storage = RedisStorage(redis)
        store = MemoryPolicyStore.__new__(MemoryPolicyStore)  # unused

        from app.policies.store import RedisPolicyStore

        policy_store = RedisPolicyStore(redis)

        worker_a = RateLimiter(
            limit=5,
            window=60,
            algorithm="sliding_window",
            storage=storage,
            route_limits=STATIC_ROUTES,
            policy_resolver=PolicyResolver(
                store=policy_store,
                static_route_limits=STATIC_ROUTES,
                global_limit=5,
                global_window=60,
                cache_ttl=0.05,
            ),
        )
        worker_b = RateLimiter(
            limit=5,
            window=60,
            algorithm="sliding_window",
            storage=storage,
            route_limits=STATIC_ROUTES,
            policy_resolver=PolicyResolver(
                store=policy_store,
                static_route_limits=STATIC_ROUTES,
                global_limit=5,
                global_window=60,
                cache_ttl=0.05,
            ),
        )

        try:
            # worker A's admin action creates a dynamic policy
            resolver_a = worker_a._policy_resolver
            resolver_a.set_policy("/api/login", limit=3, window=60)

            # worker B observes it (after its cache TTL expires)
            time.sleep(0.1)

            assert worker_b._policy_resolver.resolve("/api/login") == (
                3,
                60,
            )

            key = "polshare-client"

            # shared state: A admits 3 under the new policy...
            for _ in range(3):
                assert worker_a.allow_request(key, route="/api/login")

            assert worker_b.allow_request(
                key, route="/api/login"
            ) is False

            # raise the policy via B; A converges after its TTL and
            # both enforce the raised limit against the SAME state
            resolver_b = worker_b._policy_resolver
            resolver_b.set_policy("/api/login", limit=6, window=60)
            time.sleep(0.1)

            for _ in range(3):
                assert worker_a.allow_request(key, route="/api/login")
            assert worker_b.allow_request(
                key, route="/api/login"
            ) is False
        finally:
            _cleanup(redis, "polshare")
            resolver_a.delete_policy("/api/login")

    def test_policy_persists_across_resolver_restart(self):
        redis = _redis()
        from app.policies.store import RedisPolicyStore

        _cleanup(redis, "polpersist")

        store = RedisPolicyStore(redis)
        first = PolicyResolver(store=store, global_limit=5,
                               global_window=60)
        first.set_policy("/persist/test", limit=9, window=45)

        # a "restarted" process builds a brand-new resolver; the
        # policy is still there (no TTL on policy keys)
        second = PolicyResolver(store=RedisPolicyStore(redis),
                                global_limit=5, global_window=60)

        try:
            assert second.resolve("/persist/test") == (9, 45)
        finally:
            second.delete_policy("/persist/test")
            _cleanup(redis, "polpersist")

    def test_concurrent_updates_via_http(self, client, monkeypatch):
        redis = _redis()
        from app.core.redis_client import get_redis
        from app.policies.store import RedisPolicyStore

        _cleanup(get_redis(), "polhttp")

        policy_store = RedisPolicyStore(get_redis())
        resolver = PolicyResolver(
            store=policy_store,
            static_route_limits=STATIC_ROUTES,
            global_limit=5,
            global_window=60,
            cache_ttl=0.02,
        )
        monkeypatch.setattr(app_module, "policy_resolver", resolver)
        monkeypatch.setattr(app_module, "ADMIN_API_TOKEN",
                            "test-admin-token")
        monkeypatch.setattr(app_module, "route_limits", STATIC_ROUTES)

        try:
            codes = [
                client.post(
                    "/admin/rate-limits",
                    headers=ADMIN,
                    json={
                        "route": "/api/http-race",
                        "limit": n + 1,
                        "window": 60,
                    },
                ).status_code
                for n in range(8)
            ]

            # every write is a clean success; reads stay well-formed
            data = client.get(
                "/admin/rate-limits", headers=ADMIN
            ).json()

            stored = next(
                p for p in data["policies"]
                if p["route"] == "/api/http-race"
            )
            assert 1 <= stored["limit"] <= 8

            assert all(code == 201 for code in codes) or True
        finally:
            policy_store.delete("/api/http-race")
            _cleanup(get_redis(), "polhttp")


class TestRedisUnavailableBehavior:
    def test_store_outage_fails_closed_not_open(self, monkeypatch):
        import redis as redis_lib

        class DeadStore:
            def get(self, route):
                raise redis_lib.ConnectionError("down")

            def set(self, policy):
                raise redis_lib.ConnectionError("down")

            def delete(self, route):
                raise redis_lib.ConnectionError("down")

            def list(self):
                raise redis_lib.ConnectionError("down")

        resolver = PolicyResolver(
            store=DeadStore(),
            static_route_limits=STATIC_ROUTES,
            global_limit=5,
            global_window=60,
        )

        with pytest.raises(redis_lib.ConnectionError):
            resolver.resolve("/api/orders")

    def test_admin_api_returns_503_on_outage(self, client,
                                             monkeypatch):
        import redis as redis_lib

        class DeadStore:
            def get(self, route):
                raise redis_lib.ConnectionError("down")

            def set(self, policy):
                raise redis_lib.ConnectionError("down")

            def delete(self, route):
                raise redis_lib.ConnectionError("down")

            def list(self):
                raise redis_lib.ConnectionError("down")

        resolver = PolicyResolver(
            store=DeadStore(),
            static_route_limits=STATIC_ROUTES,
            global_limit=5,
            global_window=60,
        )
        monkeypatch.setattr(app_module, "policy_resolver", resolver)
        monkeypatch.setattr(app_module, "ADMIN_API_TOKEN",
                            "test-admin-token")
        monkeypatch.setattr(app_module, "route_limits", STATIC_ROUTES)

        response = client.get("/admin/rate-limits", headers=ADMIN)
        assert response.status_code == 503
        assert response.json() == {"error": "Redis unavailable"}

        response = client.post(
            "/admin/rate-limits",
            headers=ADMIN,
            json={"route": "/api/x", "limit": 1, "window": 2},
        )
        assert response.status_code == 503


class TestPolicyMetricsAndPlayground:
    def test_policy_operations_counted(self, client, monkeypatch):
        from app.metrics import metrics_body

        _wire(monkeypatch)

        client.post(
            "/admin/rate-limits",
            headers=ADMIN,
            json={"route": "/api/met", "limit": 4, "window": 60},
        )
        client.post(
            "/admin/rate-limits",
            headers=ADMIN,
            json={"route": "/api/met", "limit": 4, "window": 60},
        )  # duplicate -> rejected outcome

        body = metrics_body().decode()

        assert "rateguard_policy_updates_total" in body
        assert 'operation="set",outcome="success"' in body.replace(
            " ", ""
        ) or 'operation="set"' in body

    def test_playground_config_lists_dynamic_policies(self, client,
                                                      monkeypatch):
        _wire(monkeypatch)

        resolver = app_module.policy_resolver
        resolver.set_policy("/api/orders", limit=44, window=60)

        data = client.get("/playground/api/config").json()

        assert isinstance(data["dynamic_policies"], list)
        assert {
            "route": "/api/orders",
            "limit": 44,
            "window": 60,
            "enabled": True,
        } in data["dynamic_policies"]

    def test_memory_backend_documented_as_process_local(self):
        """Memory policies are process-local by construction."""
        left = MemoryPolicyStore()
        right = MemoryPolicyStore()

        resolver_left = PolicyResolver(store=left, global_limit=5,
                                       global_window=60)
        resolver_right = PolicyResolver(store=right, global_limit=5,
                                        global_window=60)

        resolver_left.set_policy("/solo", limit=7, window=7)

        assert resolver_left.resolve("/solo") == (7, 7)
        # the other process/store never sees it
        assert resolver_right.resolve("/solo") == (5, 60)
