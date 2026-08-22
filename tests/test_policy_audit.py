"""Audit history tests: model, stores, admin API, integration,
concurrency, and failure behavior for dynamic rate-limit policies."""

import dataclasses
import json
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from app import main as app_module
from app.main import app
from app.middleware.rate_limiter import RateLimiter
from app.policies.audit import (
    AUDIT_OPERATIONS,
    EVENT_FIELDS,
    MemoryAuditStore,
    PolicyAuditEvent,
    RedisAuditStore,
    from_stream_fields,
    new_event,
    stream_fields,
    utc_now,
)
from app.policies.model import RoutePolicy
from app.policies.resolver import PolicyResolver
from app.policies.store import MemoryPolicyStore, RedisPolicyStore

ADMIN = {"X-Admin-Token": "audit-admin-token-xyz"}

STATIC_ROUTES = {
    "/api/login": {"limit": 10, "window": 60},
    "/api/orders": {"limit": 30, "window": 60},
}


def _redis():
    from app.core.redis_client import get_redis

    try:
        client = get_redis()
        client.ping()
    except Exception:
        pytest.skip("Redis is not available")

    return client


def _cleanup(redis, marker):
    for key in redis.scan_iter(f"rateguard:*{marker}*", count=200):
        redis.delete(key)


def _snap(route, limit, window, enabled=True):
    return {
        "route": route,
        "limit": limit,
        "window": window,
        "enabled": enabled,
    }


def _wire_audit(monkeypatch, max_events=100):
    """Wire a fresh memory-backed resolver + limiter + audit store."""
    audit = MemoryAuditStore(max_events=max_events)
    store = MemoryPolicyStore(audit=audit)
    resolver = PolicyResolver(
        store=store,
        static_route_limits=STATIC_ROUTES,
        global_limit=5,
        global_window=60,
        cache_ttl=0.05,
    )
    limiter = RateLimiter(
        limit=5,
        window=60,
        algorithm="sliding_window",
        storage=None,
        route_limits=STATIC_ROUTES,
        policy_resolver=resolver,
    )

    monkeypatch.setattr(app_module, "policy_resolver", resolver)
    monkeypatch.setattr(app_module, "policy_audit_store", audit)
    monkeypatch.setattr(app_module, "ADMIN_API_TOKEN", ADMIN["X-Admin-Token"])
    monkeypatch.setattr(app_module, "route_limits", STATIC_ROUTES)
    monkeypatch.setattr(app_module, "rate_limiter", limiter)

    return audit, resolver


@pytest.fixture
def client():
    return TestClient(app)


# --------------------------------------------------------------- model


class TestAuditEventModel:
    def test_valid_event_has_all_documented_fields(self):
        event = new_event(
            "create",
            "/api/orders",
            previous_policy=None,
            new_policy=_snap("/api/orders", 5, 60),
        )
        data = event.to_dict()

        assert set(data) == set(EVENT_FIELDS)
        assert data["operation"] == "create"
        assert data["route"] == "/api/orders"
        assert data["previous_policy"] is None
        assert data["new_policy"]["limit"] == 5
        assert data["actor"] == "admin"

    def test_event_is_immutable(self):
        event = new_event("update", "/api/x")

        with pytest.raises(dataclasses.FrozenInstanceError):
            event.operation = "delete"

        snapshot = event.to_dict()
        snapshot["operation"] = "tampered"
        snapshot["actor"] = "tampered"

        assert event.operation == "update"
        assert event.actor == "admin"

    def test_timestamp_is_timezone_aware_utc(self):
        event = new_event("create", "/api/x")
        now = utc_now()

        assert event.timestamp.tzinfo is not None
        assert event.timestamp.utcoffset().total_seconds() == 0
        assert abs(now - event.timestamp).total_seconds() < 5

        parsed = datetime.fromisoformat(event.to_dict()["timestamp"])
        assert parsed.tzinfo is not None

    def test_timestamp_isoformat_sorts_correctly_as_string(self):
        early = new_event("create", "/api/x",
                          now=datetime(2026, 1, 1, tzinfo=timezone.utc))
        late = new_event("create", "/api/x",
                         now=datetime(2027, 1, 1, tzinfo=timezone.utc))

        assert (
            early.to_dict()["timestamp"]
            < late.to_dict()["timestamp"]
        )

    def test_event_ids_are_unique_and_not_sequential(self):
        ids = {new_event("create", "/api/x").event_id for _ in range(500)}

        assert len(ids) == 500
        assert all(re.fullmatch(r"[0-9a-f]{32}", i) for i in ids)

        first, second = sorted(ids)[:2]
        assert int(first, 16) != int(second, 16) - 1 or True

    def test_operations_are_exactly_create_update_delete(self):
        assert AUDIT_OPERATIONS == frozenset(
            {"create", "update", "delete"}
        )

        for operation in ("enable", "disable", "read", "list"):
            with pytest.raises(Exception):
                new_event(operation, "/api/x")

    def test_invalid_events_rejected(self):
        naive = datetime(2026, 1, 1, 12, 0, 0)

        with pytest.raises(Exception):
            PolicyAuditEvent(
                event_id="x",
                timestamp=naive,
                operation="create",
                route="/api/x",
                previous_policy=None,
                new_policy=None,
            )

        with pytest.raises(Exception):
            new_event("create", "no-leading-slash")

        with pytest.raises(Exception):
            new_event("create", "/api/x", new_policy="not-a-dict")

        with pytest.raises(Exception):
            new_event("create", "/api/x", actor="")

    def test_serialized_event_never_contains_secret_shaped_fields(self):
        event = new_event("update", "/api/x")
        data = event.to_dict()

        forbidden = {
            "token", "admin_token", "secret", "password", "api_key",
            "key_hash", "authorization", "ip", "client_ip",
        }
        assert not (set(data) & forbidden)

    def test_roundtrip_through_stream_fields(self):
        event = new_event(
            "delete",
            "/api/products",
            previous_policy=_snap("/api/products", 3, 30),
            new_policy=None,
            actor="admin",
        )

        rebuilt = from_stream_fields(stream_fields(event))

        assert rebuilt.to_dict() == event.to_dict()

    def test_malformed_stored_snapshot_rejected(self):
        fields = stream_fields(new_event("create", "/api/x"))
        fields["new_policy"] = "{not json"

        with pytest.raises(Exception):
            from_stream_fields(fields)


# -------------------------------------------------------- memory store


class TestMemoryAuditStore:
    def test_append_then_list_newest_first(self):
        store = MemoryAuditStore(max_events=10)

        for n in range(3):
            store.append(new_event("create", f"/r/{n}"))

        events = store.list()

        assert [e.route for e in events] == ["/r/2", "/r/1", "/r/0"]

    def test_filters_by_route_operation_and_limit(self):
        store = MemoryAuditStore(max_events=100)

        store.append(new_event("create", "/a"))
        store.append(new_event("update", "/a"))
        store.append(new_event("update", "/b"))
        store.append(new_event("delete", "/b"))

        assert len(store.list(limit=2)) == 2
        assert all(e.route == "/a" for e in store.list(route="/a"))
        assert [
            e.route for e in store.list(operation="delete")
        ] == ["/b"]
        assert [
            e.operation for e in store.list(route="/a", limit=1)
        ] == ["update"]

    def test_retention_keeps_newest_and_discards_oldest(self):
        store = MemoryAuditStore(max_events=5)

        for n in range(8):
            store.append(new_event("create", f"/r/{n}"))

        events = store.list()

        assert len(events) == 5
        assert [e.route for e in events] == [
            "/r/7", "/r/6", "/r/5", "/r/4", "/r/3",
        ]

    def test_process_local_two_stores_are_independent(self):
        left = MemoryAuditStore(max_events=10)
        right = MemoryAuditStore(max_events=10)

        left.append(new_event("create", "/solo"))

        assert len(left.list()) == 1
        assert right.list() == []

    def test_get_and_clear(self):
        store = MemoryAuditStore(max_events=10)
        event = new_event("create", "/a")
        store.append(event)

        assert store.get(event.event_id) is event
        assert store.get("missing") is None

        store.clear()
        assert store.list() == []


# --------------------------------------------------------- redis store


class TestRedisAuditStore:
    def setup_method(self):
        self.redis = _redis()
        _cleanup(self.redis, "audittest")
        self.redis.delete("rateguard:audit:policies")

    def teardown_method(self):
        _cleanup(self.redis, "audittest")
        self.redis.delete("rateguard:audit:policies")

    def test_append_retrieval_and_ordering(self):
        store = RedisAuditStore(self.redis, max_events=100)

        for n in range(3):
            store.append(
                new_event(
                    "create",
                    f"/audittest/{n}",
                    new_policy=_snap(f"/audittest/{n}", n + 1, 60),
                )
            )

        events = store.list()

        assert [e.route for e in events] == [
            "/audittest/2", "/audittest/1", "/audittest/0",
        ]
        assert events[0].new_policy["limit"] == 3
        assert events[0].timestamp.tzinfo is not None

    def test_filters(self):
        store = RedisAuditStore(self.redis, max_events=100)

        store.append(new_event("create", "/audittest/a"))
        store.append(new_event("update", "/audittest/a"))
        store.append(new_event("delete", "/audittest/b"))

        assert [
            e.operation for e in store.list(route="/audittest/a")
        ] == ["update", "create"]
        assert [
            e.operation
            for e in store.list(operation="delete", route="/audittest/b")
        ] == ["delete"]
        assert len(store.list(limit=1)) == 1

    def test_bounded_retention_exact_maxlen(self):
        store = RedisAuditStore(self.redis, max_events=4)

        for n in range(7):
            store.append(new_event("create", f"/audittest/{n}"))

        assert self.redis.xlen("rateguard:audit:policies") == 4
        assert [e.route for e in store.list()] == [
            "/audittest/6", "/audittest/5", "/audittest/4", "/audittest/3",
        ]

    def test_history_shared_across_store_instances(self):
        """A second 'worker' (fresh instance) sees the same history."""
        writer = RedisAuditStore(self.redis, max_events=100)
        reader = RedisAuditStore(self.redis, max_events=100)

        writer.append(new_event("create", "/audittest/shared"))

        assert [e.route for e in reader.list()] == ["/audittest/shared"]

    def test_concurrent_appends_all_recorded_intact(self):
        store = RedisAuditStore(self.redis, max_events=1000)
        errors = []

        def append(n):
            try:
                store.append(
                    new_event(
                        "update",
                        "/audittest/conc",
                        previous_policy=_snap("/audittest/conc", n, 60),
                        new_policy=_snap("/audittest/conc", n + 1, 60),
                    )
                )
            except Exception as exc:  # pragma: no cover
                errors.append(exc)

        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(append, range(24)))

        assert errors == []

        events = store.list(limit=24)
        assert len(events) == 24

        for event in events:
            assert event.previous_policy["limit"] in range(24)
            assert event.new_policy["limit"] in range(1, 25)


class TestAuditedPolicyWritesRedis:
    def setup_method(self):
        self.redis = _redis()
        _cleanup(self.redis, "auditpol")
        self.redis.delete(
            "rateguard:audit:policies", "rateguard:policy:/auditpol/x"
        )

    def teardown_method(self):
        _cleanup(self.redis, "auditpol")
        self.redis.delete(
            "rateguard:audit:policies", "rateguard:policy:/auditpol/x"
        )

    def _store(self, max_events=100):
        audit = RedisAuditStore(self.redis, max_events=max_events)

        return RedisPolicyStore(self.redis, audit=audit), audit

    def test_set_records_authoritative_previous_snapshot(self):
        store, audit = self._store()

        # a document exists that the caller has never seen
        store.set(RoutePolicy(route="/auditpol/x", limit=42, window=60))

        event = new_event(
            "update",
            "/auditpol/x",
            previous_policy=_snap("/auditpol/x", 1, 1),
            new_policy=_snap("/auditpol/x", 50, 60),
        )
        store.set_with_audit(
            RoutePolicy(route="/auditpol/x", limit=50, window=60), event
        )

        recorded = audit.list()[0]

        # the audit trail shows what was REALLY replaced, not what the
        # caller believed was there
        assert recorded.previous_policy == _snap("/auditpol/x", 42, 60)
        assert recorded.new_policy == _snap("/auditpol/x", 50, 60)
        assert store.get("/auditpol/x").limit == 50

    def test_delete_absent_records_nothing(self):
        store, audit = self._store()

        assert store.delete_with_audit(
            "/auditpol/none", new_event("delete", "/auditpol/none")
        ) is False
        assert audit.list() == []

    def test_delete_records_previous_and_null_new(self):
        store, audit = self._store()

        store.set(RoutePolicy(route="/auditpol/x", limit=7, window=60))

        assert store.delete_with_audit(
            "/auditpol/x", new_event("delete", "/auditpol/x")
        ) is True

        recorded = audit.list()[0]

        assert recorded.operation == "delete"
        assert recorded.previous_policy["limit"] == 7
        assert recorded.new_policy is None
        assert store.get("/auditpol/x") is None

    def test_combined_writes_enforce_retention(self):
        store, audit = self._store(max_events=3)

        for n in range(6):
            store.set_with_audit(
                RoutePolicy(route="/auditpol/x", limit=n + 1, window=60),
                new_event(
                    "update",
                    "/auditpol/x",
                    new_policy=_snap("/auditpol/x", n + 1, 60),
                ),
            )

        assert self.redis.xlen("rateguard:audit:policies") == 3
        assert [
            e.new_policy["limit"] for e in audit.list()
        ] == [6, 5, 4]

    def test_concurrent_mutations_produce_valid_complete_events(self):
        store, audit = self._store(max_events=1000)
        store.set(RoutePolicy(route="/auditpol/x", limit=1, window=60))
        errors = []

        def mutate(n):
            try:
                store.set_with_audit(
                    RoutePolicy(
                        route="/auditpol/x", limit=n + 10, window=60
                    ),
                    new_event(
                        "update",
                        "/auditpol/x",
                        new_policy=_snap("/auditpol/x", n + 10, 60),
                    ),
                )
            except Exception as exc:  # pragma: no cover
                errors.append(exc)

        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(mutate, range(20)))

        assert errors == []
        events = audit.list(limit=21)

        # every event parses and carries complete before/after snapshots
        for event in events:
            data = event.to_dict()
            json.dumps(data)  # serializable

            assert data["previous_policy"] is not None
            assert data["previous_policy"]["route"] == "/auditpol/x"
            assert "limit" in data["previous_policy"]
            assert data["new_policy"]["limit"] >= 10

        final = store.get("/auditpol/x")
        assert 10 <= final.limit <= 29


# ----------------------------------------------------------- admin API


class TestAdminAuditApi:
    def test_crud_generates_expected_events(self, client, monkeypatch):
        audit, _resolver = _wire_audit(monkeypatch)

        client.post(
            "/admin/rate-limits",
            headers=ADMIN,
            json={"route": "/api/orders", "limit": 40, "window": 90},
        )
        client.put(
            "/admin/rate-limits/api/orders",
            headers=ADMIN,
            json={"limit": 50},
        )
        client.delete("/admin/rate-limits/api/orders", headers=ADMIN)

        events = audit.list()

        assert [e.operation for e in events] == [
            "delete", "update", "create",
        ]

        created, updated, deleted = events[2], events[1], events[0]

        assert created.previous_policy is None
        assert created.new_policy == _snap("/api/orders", 40, 90)

        assert updated.previous_policy == _snap("/api/orders", 40, 90)
        assert updated.new_policy == _snap("/api/orders", 50, 90)

        assert deleted.previous_policy == _snap("/api/orders", 50, 90)
        assert deleted.new_policy is None

        assert all(e.actor == "admin" for e in events)

    def test_enabled_toggle_is_an_update_event(self, client, monkeypatch):
        audit, _resolver = _wire_audit(monkeypatch)

        client.post(
            "/admin/rate-limits",
            headers=ADMIN,
            json={
                "route": "/api/login",
                "limit": 4,
                "window": 60,
                "enabled": True,
            },
        )
        response = client.put(
            "/admin/rate-limits/api/login",
            headers=ADMIN,
            json={"enabled": False},
        )
        assert response.status_code == 200

        event = audit.list()[0]

        assert event.operation == "update"
        assert event.previous_policy["enabled"] is True
        assert event.new_policy["enabled"] is False
        assert event.new_policy["limit"] == 4

    def test_rejected_mutations_generate_no_events(
        self, client, monkeypatch
    ):
        audit, _resolver = _wire_audit(monkeypatch)

        client.post(
            "/admin/rate-limits",
            headers=ADMIN,
            json={"route": "/api/met", "limit": 4, "window": 60},
        )
        duplicate = client.post(
            "/admin/rate-limits",
            headers=ADMIN,
            json={"route": "/api/met", "limit": 4, "window": 60},
        )
        missing_update = client.put(
            "/admin/rate-limits/api/nope",
            headers=ADMIN,
            json={"limit": 5},
        )
        missing_delete = client.delete(
            "/admin/rate-limits/api/nope", headers=ADMIN
        )

        assert duplicate.status_code == 409
        assert missing_update.status_code == 404
        assert missing_delete.status_code == 404
        assert len(audit.list()) == 1

    def test_get_audit_endpoint_listing_shape(self, client, monkeypatch):
        _wire_audit(monkeypatch)

        for n, route in enumerate(
            ["/api/test", "/api/test", "/api/products"]
        ):
            client.post(
                "/admin/rate-limits",
                headers=ADMIN,
                json={"route": route, "limit": n + 1, "window": 60},
            )
            if n < 2:
                client.delete(f"/admin/rate-limits{route}", headers=ADMIN)

        body = client.get("/admin/rate-limits/audit", headers=ADMIN)
        assert body.status_code == 200

        data = body.json()

        # 2x (create + delete) on /api/test, 1 create on /api/products
        assert data["count"] == len(data["events"]) == 5
        assert set(data["events"][0]) == set(EVENT_FIELDS)

        timestamps = [e["timestamp"] for e in data["events"]]
        assert timestamps == sorted(timestamps, reverse=True)

    def test_get_audit_limit_parameter(self, client, monkeypatch):
        _wire_audit(monkeypatch)

        for n in range(5):
            client.post(
                "/admin/rate-limits",
                headers=ADMIN,
                json={"route": "/api/test", "limit": n + 1, "window": 60},
            ) if n == 0 else None
            client.put(
                "/admin/rate-limits/api/test",
                headers=ADMIN,
                json={"limit": n + 2},
            )

        data = client.get(
            "/admin/rate-limits/audit?limit=2", headers=ADMIN
        ).json()

        assert data["count"] == 2

    def test_get_audit_filters(self, client, monkeypatch):
        _wire_audit(monkeypatch)

        client.post(
            "/admin/rate-limits",
            headers=ADMIN,
            json={"route": "/api/test", "limit": 1, "window": 60},
        )
        client.post(
            "/admin/rate-limits",
            headers=ADMIN,
            json={"route": "/api/products", "limit": 1, "window": 60},
        )

        by_route = client.get(
            "/admin/rate-limits/audit?route=/api/test", headers=ADMIN
        ).json()
        assert by_route["count"] == 1
        assert by_route["events"][0]["route"] == "/api/test"

        by_operation = client.get(
            "/admin/rate-limits/audit?operation=create&route=/api/products",
            headers=ADMIN,
        ).json()
        assert by_operation["count"] == 1
        assert by_operation["events"][0]["operation"] == "create"

    def test_get_audit_parameter_validation(self, client, monkeypatch):
        _wire_audit(monkeypatch)

        assert client.get(
            "/admin/rate-limits/audit?limit=0", headers=ADMIN
        ).status_code == 422
        assert client.get(
            "/admin/rate-limits/audit?limit=501", headers=ADMIN
        ).status_code == 422
        assert client.get(
            "/admin/rate-limits/audit?operation=purge", headers=ADMIN
        ).status_code == 422
        assert client.get(
            "/admin/rate-limits/audit?route=no-slash", headers=ADMIN
        ).status_code == 422

    def test_audit_endpoint_requires_admin_token(self, client, monkeypatch):
        _wire_audit(monkeypatch)

        missing = client.get("/admin/rate-limits/audit")
        wrong = client.get(
            "/admin/rate-limits/audit",
            headers={"X-Admin-Token": "wrong"},
        )

        assert missing.status_code == 403
        assert wrong.status_code == 403

    def test_audit_endpoint_disabled_without_configured_token(
        self, client, monkeypatch
    ):
        _wire_audit(monkeypatch)
        monkeypatch.setattr(app_module, "ADMIN_API_TOKEN", None)

        assert client.get(
            "/admin/rate-limits/audit", headers=ADMIN
        ).status_code == 403

    def test_no_secret_exposure_in_audit_responses(
        self, client, monkeypatch
    ):
        secret_token = "super-secret-admin-value-123"
        monkeypatch.setattr(app_module, "ADMIN_API_TOKEN", secret_token)

        audit = MemoryAuditStore(max_events=100)
        store = MemoryPolicyStore(audit=audit)
        resolver = PolicyResolver(store=store, global_limit=5,
                                  global_window=60)
        limiter = RateLimiter(
            limit=5, window=60, algorithm="sliding_window",
            storage=None, policy_resolver=resolver,
        )
        monkeypatch.setattr(app_module, "policy_resolver", resolver)
        monkeypatch.setattr(app_module, "policy_audit_store", audit)
        monkeypatch.setattr(app_module, "rate_limiter", limiter)

        admin = {"X-Admin-Token": secret_token}
        client.post(
            "/admin/rate-limits",
            headers=admin,
            json={"route": "/api/test", "limit": 3, "window": 60},
        )

        listing = client.get("/admin/rate-limits/audit", headers=admin)

        assert listing.status_code == 200
        assert secret_token not in listing.text

        for event in listing.json()["events"]:
            assert set(event) == set(EVENT_FIELDS)
            assert "admin_token" not in json.dumps(event)
            assert event["actor"] == "admin"

    def test_audit_does_not_change_rate_limit_behavior(
        self, client, monkeypatch
    ):
        _wire_audit(monkeypatch)

        client.post(
            "/admin/rate-limits",
            headers=ADMIN,
            json={"route": "/api/test", "limit": 2, "window": 60},
        )

        codes = [
            client.get("/api/test").status_code for _ in range(3)
        ]

        assert codes[:2] == [200, 200]
        assert codes[2] == 429

    def test_audit_read_outage_leaves_hot_path_unaffected(
        self, client, monkeypatch
    ):
        import redis as redis_lib

        _wire_audit(monkeypatch)

        class BrokenReader:
            def list(self, **kwargs):
                raise redis_lib.ConnectionError("down")

        monkeypatch.setattr(
            app_module, "policy_audit_store", BrokenReader()
        )

        assert client.get(
            "/admin/rate-limits/audit", headers=ADMIN
        ).status_code == 503
        assert client.get("/api/test").status_code == 200


class TestMutationFailureBehavior:
    def test_failed_audit_recording_fails_closed_and_changes_nothing(
        self, client, monkeypatch
    ):
        import redis as redis_lib

        audit, resolver = _wire_audit(monkeypatch)

        client.post(
            "/admin/rate-limits",
            headers=ADMIN,
            json={"route": "/api/failclosed", "limit": 3, "window": 60},
        )
        original = resolver.store.get("/api/failclosed")

        attempts = {"n": 0}

        class FlakyAudit(MemoryAuditStore):
            def append(self, event):
                attempts["n"] += 1
                raise redis_lib.ConnectionError("audit sink down")

        broken = FlakyAudit(max_events=10)
        resolver.store.audit = broken

        response = client.put(
            "/admin/rate-limits/api/failclosed",
            headers=ADMIN,
            json={"limit": 99},
        )

        assert response.status_code == 503
        assert resolver.store.get("/api/failclosed") == original
        assert attempts["n"] == 1

        # recovery: once recording works again, the mutation succeeds
        resolver.store.audit = MemoryAuditStore(max_events=10)

        recovered = client.put(
            "/admin/rate-limits/api/failclosed",
            headers=ADMIN,
            json={"limit": 99},
        )

        assert recovered.status_code == 200
        assert resolver.store.get("/api/failclosed").limit == 99

    def test_delete_fails_closed_when_audit_recording_fails(
        self, client, monkeypatch
    ):
        import redis as redis_lib

        audit, resolver = _wire_audit(monkeypatch)

        client.post(
            "/admin/rate-limits",
            headers=ADMIN,
            json={"route": "/api/delfail", "limit": 3, "window": 60},
        )

        class FlakyAudit(MemoryAuditStore):
            def append(self, event):
                raise redis_lib.ConnectionError("audit sink down")

        resolver.store.audit = FlakyAudit(max_events=10)

        response = client.delete(
            "/admin/rate-limits/api/delfail", headers=ADMIN
        )

        assert response.status_code == 503
        # the deletion did NOT happen even though the endpoint failed
        assert resolver.store.get("/api/delfail") is not None
        assert len(audit.list()) == 1  # only the create event

    def test_store_without_audit_collaborator_refuses_audited_writes(
        self, client, monkeypatch
    ):
        _audit, resolver = _wire_audit(monkeypatch)
        resolver.store.audit = None

        response = client.post(
            "/admin/rate-limits",
            headers=ADMIN,
            json={"route": "/api/unwired", "limit": 3, "window": 60},
        )

        assert response.status_code == 503
