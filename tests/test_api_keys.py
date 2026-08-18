import pytest
from fastapi.testclient import TestClient

from app import main as app_module
from app.api_keys import ApiKeyStore, RedisApiKeyStore
from app.main import app
from app.middleware.rate_limiter import RateLimiter

ROUTES = {
    "/api/login": {"limit": 10, "window": 60},
    "/api/products": {"limit": 200, "window": 60},
}


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture
def key_store(monkeypatch):
    """Monkeypatch the app's store with a fresh in-memory one."""
    store = ApiKeyStore()

    monkeypatch.setattr(app_module, "api_key_store", store)

    return store


@pytest.fixture
def admin_token(monkeypatch):
    """Enable admin auth with a known token for the duration of a test."""
    monkeypatch.setattr(app_module, "ADMIN_API_TOKEN", "test-admin-token")

    return "test-admin-token"


class TestApiKeyStore:
    def test_create_returns_prefixed_key_and_verify(self, tmp_path):
        store = ApiKeyStore(path=str(tmp_path / "keys.json"))

        key, meta = store.create(name="app", owner="acme")

        assert key.startswith("rg_live_")
        assert meta["enabled"] is True
        assert store.verify(key) is not None
        assert store.verify("rg_live_totally_wrong") is None

    def test_stores_only_hash(self, tmp_path):
        store = ApiKeyStore(path=str(tmp_path / "keys.json"))

        key, _ = store.create(name="app")

        record = store.verify(key)
        assert record["hash"] != key

        raw = (tmp_path / "keys.json").read_text(encoding="utf-8")
        assert key not in raw

    def test_memory_store(self):
        store = ApiKeyStore()

        key, _ = store.create(name="app")

        assert store.verify(key) is not None

    def test_authenticate_rejects_disabled(self, tmp_path):
        store = ApiKeyStore(path=str(tmp_path / "keys.json"))

        key, meta = store.create(name="app")

        assert store.authenticate(key) is not None

        store.revoke(meta["id"])

        assert store.verify(key) is not None
        assert store.authenticate(key) is None

    def test_authenticate_rejects_expired(self, tmp_path):
        store = ApiKeyStore(path=str(tmp_path / "keys.json"))

        key, _ = store.create(name="app", ttl=-1)

        assert store.authenticate(key) is None

    def test_list_metadata_has_no_secret(self, tmp_path):
        store = ApiKeyStore(path=str(tmp_path / "keys.json"))

        key, meta = store.create(name="app", owner="acme")

        listed = store.list()
        assert len(listed) == 1

        item = listed[0]
        assert "hash" not in item
        assert "key" not in item
        assert key not in str(listed)
        assert item["id"] == meta["id"]
        assert item["name"] == "app"

    def test_delete(self, tmp_path):
        store = ApiKeyStore(path=str(tmp_path / "keys.json"))

        key, meta = store.create(name="app")

        assert store.delete(meta["id"]) is True
        assert store.verify(key) is None
        assert store.delete(meta["id"]) is False

    def test_persistence_across_reload(self, tmp_path):
        path = str(tmp_path / "keys.json")
        store = ApiKeyStore(path=path)

        key, _ = store.create(name="app")

        reloaded = ApiKeyStore(path=path)
        assert reloaded.verify(key) is not None


class TestRedisApiKeyStore:
    @staticmethod
    def _store():
        from app.core.redis_client import get_redis

        try:
            get_redis().ping()
        except Exception:
            pytest.skip("Redis is not available")

        return RedisApiKeyStore(get_redis())

    @staticmethod
    def _cleanup(store):
        for found in store.redis_client.scan_iter("rateguard:apikey*"):
            store.redis_client.delete(found)

    def test_roundtrip(self):
        store = self._store()

        key, meta = store.create(name="app", owner="acme")

        try:
            assert key.startswith("rg_live_")
            assert store.authenticate(key) is not None
            assert store.verify("rg_live_wrong") is None

            store.revoke(meta["id"])
            assert store.authenticate(key) is None

            store.delete(meta["id"])
            assert store.verify(key) is None
        finally:
            self._cleanup(store)

    def test_list_excludes_secret(self):
        store = self._store()

        key, meta = store.create(name="app", owner="acme")

        try:
            listed = store.list()

            assert any(item["id"] == meta["id"] for item in listed)
            assert key not in str(listed)
        finally:
            self._cleanup(store)


class TestAuthentication:
    def test_valid_api_key_gets_headers(self, client, key_store):
        key, _ = key_store.create(name="app", owner="owner-valid")

        response = client.get(
            "/api/test",
            headers={"X-API-Key": key}
        )

        assert response.status_code == 200
        assert response.headers["X-RateLimit-Limit"] == str(app_module.limit)
        assert "X-RateLimit-Remaining" in response.headers
        assert "X-RateLimit-Reset" in response.headers

    def test_missing_api_key_falls_back_to_ip(self, client, key_store):
        response = client.get("/api/test")

        assert response.status_code == 200
        assert response.headers["X-RateLimit-Limit"] == str(app_module.limit)

    def test_invalid_api_key_returns_401(self, client, key_store):
        response = client.get(
            "/api/test",
            headers={"X-API-Key": "rg_live_does_not_exist"}
        )

        assert response.status_code == 401

    def test_disabled_api_key_returns_401(self, client, key_store):
        key, meta = key_store.create(name="app", owner="owner-disabled")
        key_store.revoke(meta["id"])

        response = client.get(
            "/api/test",
            headers={"X-API-Key": key}
        )

        assert response.status_code == 401

    def test_expired_api_key_returns_401(self, client, key_store):
        key, _ = key_store.create(name="app", owner="owner-expired", ttl=-1)

        response = client.get(
            "/api/test",
            headers={"X-API-Key": key}
        )

        assert response.status_code == 401

    def test_legacy_header_value_keeps_opaque_identity(self, client, key_store):
        response = client.get(
            "/api/test",
            headers={"X-API-Key": "legacy-client-1"}
        )

        assert response.status_code == 200
        assert response.headers["X-RateLimit-Limit"] == str(app_module.limit)


class TestKeyIsolation:
    def test_two_api_keys_independent_limits(self, client, key_store):
        key_a, _ = key_store.create(name="a", owner="owner-iso-a")
        key_b, _ = key_store.create(name="b", owner="owner-iso-b")

        for _ in range(app_module.limit):
            assert (
                client.get(
                    "/api/test",
                    headers={"X-API-Key": key_a}
                ).status_code
                == 200
            )

        assert (
            client.get(
                "/api/test",
                headers={"X-API-Key": key_a}
            ).status_code
            == 429
        )

        response = client.get(
            "/api/test",
            headers={"X-API-Key": key_b}
        )
        assert response.status_code == 200

    def test_keys_without_owner_independent(self, client, key_store):
        key_a, _ = key_store.create(name="a")
        key_b, _ = key_store.create(name="b")

        for _ in range(app_module.limit):
            assert (
                client.get(
                    "/api/test",
                    headers={"X-API-Key": key_a}
                ).status_code
                == 200
            )

        assert (
            client.get(
                "/api/test",
                headers={"X-API-Key": key_a}
            ).status_code
            == 429
        )

        assert (
            client.get(
                "/api/test",
                headers={"X-API-Key": key_b}
            ).status_code
            == 200
        )

    def test_keys_with_same_owner_share_quota(self, client, key_store):
        key_a, _ = key_store.create(name="a", owner="owner-shared")
        key_b, _ = key_store.create(name="b", owner="owner-shared")

        for _ in range(app_module.limit):
            assert (
                client.get(
                    "/api/test",
                    headers={"X-API-Key": key_a}
                ).status_code
                == 200
            )

        assert (
            client.get(
                "/api/test",
                headers={"X-API-Key": key_b}
            ).status_code
            == 429
        )

    def test_same_api_key_across_routes_independent(self, client, key_store, monkeypatch):
        limiter = RateLimiter(limit=5, window=60, route_limits=ROUTES)
        monkeypatch.setattr(app_module, "rate_limiter", limiter)

        key, _ = key_store.create(name="app", owner="owner-routes")
        headers = {"X-API-Key": key}

        for _ in range(10):
            assert (
                client.post("/api/login", headers=headers).status_code
                == 200
            )

        response = client.get("/api/products", headers=headers)
        assert response.status_code == 200
        assert response.headers["X-RateLimit-Limit"] == "200"

    def test_per_route_limits_still_work(self, client, key_store, monkeypatch):
        limiter = RateLimiter(limit=5, window=60, route_limits=ROUTES)
        monkeypatch.setattr(app_module, "rate_limiter", limiter)

        key, _ = key_store.create(name="app", owner="owner-per-route")
        headers = {"X-API-Key": key}

        for _ in range(10):
            assert (
                client.post("/api/login", headers=headers).status_code
                == 200
            )

        response = client.post("/api/login", headers=headers)
        assert response.status_code == 429
        assert response.headers["X-RateLimit-Limit"] == "10"
        assert response.headers["Retry-After"] == (
            response.headers["X-RateLimit-Reset"]
        )

    def test_429_headers_and_retry_after(self, client, key_store, monkeypatch):
        limiter = RateLimiter(limit=2, window=60)
        monkeypatch.setattr(app_module, "rate_limiter", limiter)

        key, _ = key_store.create(name="app", owner="owner-429")
        headers = {"X-API-Key": key}

        assert (
            client.get("/api/test", headers=headers).status_code
            == 200
        )
        assert (
            client.get("/api/test", headers=headers).status_code
            == 200
        )

        response = client.get("/api/test", headers=headers)
        assert response.status_code == 429
        assert response.headers["X-RateLimit-Limit"] == "2"
        assert response.headers["X-RateLimit-Remaining"] == "0"
        assert int(response.headers["X-RateLimit-Reset"]) > 0
        assert (
            response.headers["Retry-After"]
            == response.headers["X-RateLimit-Reset"]
        )


class TestAdminProtection:
    def test_admin_not_configured_rejects_all(self, client, monkeypatch):
        monkeypatch.setattr(app_module, "ADMIN_API_TOKEN", None)

        response = client.get("/admin/api-keys")
        assert response.status_code == 403

    def test_missing_token_rejected(self, client, admin_token):
        response = client.get("/admin/api-keys")
        assert response.status_code == 403

    def test_wrong_token_rejected(self, client, admin_token):
        response = client.get(
            "/admin/api-keys",
            headers={"X-Admin-Token": "wrong-token"}
        )
        assert response.status_code == 403

    def test_all_admin_endpoints_protected(self, client, admin_token):
        assert (
            client.post(
                "/admin/api-keys",
                json={"name": "nope"}
            ).status_code
            == 403
        )
        assert client.get("/admin/api-keys").status_code == 403
        assert (
            client.post("/admin/api-keys/some-id/revoke").status_code
            == 403
        )
        assert (
            client.delete("/admin/api-keys/some-id").status_code
            == 403
        )

    def test_authorized_request_allowed(self, client, admin_token, key_store):
        response = client.get(
            "/admin/api-keys",
            headers={"X-Admin-Token": admin_token}
        )
        assert response.status_code == 200


class TestAdminApi:
    def test_create_returns_secret_once(self, client, admin_token, key_store):
        response = client.post(
            "/admin/api-keys",
            json={"name": "my-app", "owner": "acme"},
            headers={"X-Admin-Token": admin_token}
        )

        assert response.status_code == 201
        data = response.json()
        assert data["key"].startswith("rg_live_")
        assert data["enabled"] is True
        assert data["owner"] == "acme"
        assert "expires_at" in data

    def test_created_key_can_be_used(self, client, admin_token, key_store):
        created = client.post(
            "/admin/api-keys",
            json={"name": "my-app", "owner": "owner-created"},
            headers={"X-Admin-Token": admin_token}
        ).json()

        response = client.get(
            "/api/test",
            headers={"X-API-Key": created["key"]}
        )

        assert response.status_code == 200

    def test_list_does_not_expose_secret(self, client, admin_token, key_store):
        key, meta = key_store.create(name="my-app", owner="acme")

        listed = client.get(
            "/admin/api-keys",
            headers={"X-Admin-Token": admin_token}
        ).json()["api_keys"]

        assert any(item["id"] == meta["id"] for item in listed)
        assert key not in str(listed)

    def test_revoke_and_delete(self, client, admin_token, key_store):
        key, meta = key_store.create(name="my-app", owner="owner-admin")
        admin_headers = {"X-Admin-Token": admin_token}

        assert (
            client.get(
                "/api/test",
                headers={"X-API-Key": key}
            ).status_code
            == 200
        )

        revoke_response = client.post(
            f"/admin/api-keys/{meta['id']}/revoke",
            headers=admin_headers
        )
        assert revoke_response.status_code == 200
        assert key not in str(revoke_response.json())
        assert (
            client.get(
                "/api/test",
                headers={"X-API-Key": key}
            ).status_code
            == 401
        )

        delete_response = client.delete(
            f"/admin/api-keys/{meta['id']}",
            headers=admin_headers
        )
        assert delete_response.status_code == 204
        assert (
            client.get(
                "/api/test",
                headers={"X-API-Key": key}
            ).status_code
            == 401
        )

        assert (
            client.post(
                f"/admin/api-keys/{meta['id']}/revoke",
                headers=admin_headers
            ).status_code
            == 404
        )

    def test_list_never_exposes_secret_after_revoke(self, client, admin_token, key_store):
        key, meta = key_store.create(name="my-app", owner="acme")

        client.post(
            f"/admin/api-keys/{meta['id']}/revoke",
            headers={"X-Admin-Token": admin_token}
        )

        listed = client.get(
            "/admin/api-keys",
            headers={"X-Admin-Token": admin_token}
        ).json()["api_keys"]

        assert key not in str(listed)


class TestRedisHttp:
    def test_valid_key_redis_backend(self, client, monkeypatch):
        from app.core.redis_client import get_redis
        from app.storage.redis_storage import RedisStorage

        try:
            get_redis().ping()
        except Exception:
            pytest.skip("Redis is not available")

        store = RedisApiKeyStore(get_redis())
        limiter = RateLimiter(
            limit=5,
            window=60,
            storage=RedisStorage(get_redis())
        )

        monkeypatch.setattr(app_module, "api_key_store", store)
        monkeypatch.setattr(app_module, "rate_limiter", limiter)

        key, _ = store.create(name="app", owner="owner-redis-http")

        try:
            assert (
                client.get(
                    "/api/test",
                    headers={"X-API-Key": key}
                ).status_code
                == 200
            )

            for _ in range(5):
                client.get("/api/test", headers={"X-API-Key": key})

            response = client.get(
                "/api/test",
                headers={"X-API-Key": key}
            )
            assert response.status_code == 429
            assert response.headers["X-RateLimit-Limit"] == "5"
        finally:
            for found in get_redis().scan_iter("rateguard:apikey*"):
                get_redis().delete(found)
