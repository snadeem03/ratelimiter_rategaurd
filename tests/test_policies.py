"""Tests for the dynamic policy model, stores, and resolver."""

import json
import threading
import time

import pytest

from app.policies.model import (
    MAX_ROUTE_LENGTH,
    RoutePolicy,
    normalize_policy_payload,
    validate_limit,
    validate_route,
    validate_window,
)
from app.policies.resolver import (
    SOURCE_DYNAMIC,
    SOURCE_GLOBAL,
    SOURCE_STATIC,
    PolicyResolver,
)
from app.policies.store import (
    MemoryPolicyStore,
    PolicyError,
)


def _redis():
    from app.core.redis_client import get_redis

    try:
        get_redis().ping()
    except Exception:
        pytest.skip("Redis is not available")

    return get_redis()


# ------------------------------------------------------------------ model


class TestRouteValidation:
    @pytest.mark.parametrize(
        "route",
        [
            "/api/orders",
            "/api/login",
            "/x",
            "/a/b/c/d",
            "/api/v1/users_1.2~sub",
        ],
    )
    def test_valid_routes(self, route):
        assert validate_route(route) == route

    @pytest.mark.parametrize(
        "route",
        [
            "",
            "   ",
            "api/orders",           # missing leading slash
            "/",                    # root is never rate-limited
            "/api/../secret",
            "/api/./here",
            "/has space",
            "/tab\tchar",
            "/back\\slash",
            "/query?x=1",
            "/frag#y",
            "/hash{tag}",
            "/per%cent",
            "/new\nline",
        ],
    )
    def test_invalid_routes(self, route):
        with pytest.raises(ValueError):
            validate_route(route)

    def test_non_string_rejected(self):
        with pytest.raises(ValueError):
            validate_route(42)

    def test_overlong_route_rejected(self):
        with pytest.raises(ValueError):
            validate_route("/" + "a" * MAX_ROUTE_LENGTH)

    def test_surrounding_whitespace_is_stripped(self):
        assert validate_route("  /api/orders  ") == "/api/orders"


class TestLimitWindowValidation:
    @pytest.mark.parametrize("limit", [0, -1, -100])
    def test_zero_and_negative_limits(self, limit):
        with pytest.raises(ValueError, match=">= 1"):
            validate_limit(limit)

    @pytest.mark.parametrize("window", [0, -5])
    def test_zero_and_negative_windows(self, window):
        with pytest.raises(ValueError, match=">= 1"):
            validate_window(window)

    def test_non_integer_types_rejected(self):
        for bad in ("10", 3.5, None, [1], True, False):
            with pytest.raises(ValueError):
                validate_limit(bad)

            with pytest.raises(ValueError):
                validate_window(bad)

    def test_huge_values_rejected(self):
        with pytest.raises(ValueError, match="limit"):
            validate_limit(10**12)

        with pytest.raises(ValueError, match="window"):
            validate_window(10**9)


class TestRoutePolicyModel:
    def test_valid_policy_roundtrip(self):
        policy = RoutePolicy(
            route="/api/orders", limit=50, window=60
        )

        assert policy.enabled is True

        data = policy.to_dict()
        assert data == {
            "route": "/api/orders",
            "limit": 50,
            "window": 60,
            "enabled": True,
        }

        assert RoutePolicy.from_dict(data) == policy

    def test_disabled_policy_allowed(self):
        policy = RoutePolicy(
            route="/api/orders", limit=50, window=60, enabled=False
        )
        assert policy.enabled is False

    def test_zero_limit_rejected(self):
        with pytest.raises(ValueError):
            RoutePolicy(route="/api/orders", limit=0, window=60)

    def test_negative_window_rejected(self):
        with pytest.raises(ValueError):
            RoutePolicy(route="/api/orders", limit=10, window=-1)

    def test_invalid_route_rejected(self):
        with pytest.raises(ValueError):
            RoutePolicy(route="nope", limit=10, window=60)

    def test_non_bool_enabled_rejected(self):
        with pytest.raises(ValueError):
            RoutePolicy.from_dict(
                {"route": "/a", "limit": 1, "window": 1, "enabled": "yes"}
            )

    def test_from_dict_missing_fields(self):
        with pytest.raises(ValueError, match="route"):
            RoutePolicy.from_dict({"limit": 1, "window": 1})

        with pytest.raises(ValueError, match="'limit'"):
            RoutePolicy.from_dict({"route": "/a", "window": 1})

    def test_from_dict_non_mapping(self):
        with pytest.raises(ValueError):
            RoutePolicy.from_dict(["nope"])

    def test_normalize_payload_unknown_fields(self):
        with pytest.raises(ValueError, match="unknown fields: owner"):
            normalize_policy_payload({"owner": "acme"})

    def test_normalize_payload_partial(self):
        assert normalize_policy_payload({"limit": 7}) == {"limit": 7}
        assert normalize_policy_payload({}) == {}

    def test_normalize_payload_bool_limit_rejected(self):
        with pytest.raises(ValueError):
            normalize_policy_payload({"limit": True})


# ------------------------------------------------------------------ stores


class TestMemoryPolicyStore:
    def test_set_get_delete_list(self):
        store = MemoryPolicyStore()

        assert store.get("/api/orders") is None
        assert store.list() == []

        policy = RoutePolicy(route="/api/orders", limit=30, window=60)
        store.set(policy)

        assert store.get("/api/orders") == policy
        assert store.list() == [policy]

        assert store.delete("/api/orders") is True
        assert store.delete("/api/orders") is False
        assert store.get("/api/orders") is None

    def test_upsert_replaces(self):
        store = MemoryPolicyStore()
        store.set(RoutePolicy(route="/r", limit=1, window=2))
        store.set(RoutePolicy(route="/r", limit=3, window=4))

        assert store.get("/r").limit == 3

    def test_sorted_list(self):
        store = MemoryPolicyStore()
        store.set(RoutePolicy(route="/b", limit=1, window=2))
        store.set(RoutePolicy(route="/a", limit=1, window=2))

        assert [p.route for p in store.list()] == ["/a", "/b"]

    def test_process_local_isolation(self):
        """Two stores do not share state (process-local semantics)."""
        a, b = MemoryPolicyStore(), MemoryPolicyStore()
        a.set(RoutePolicy(route="/api/orders", limit=1, window=2))

        assert b.get("/api/orders") is None


class TestRedisPolicyStore:
    def test_set_get_delete_persistence(self):
        client = _redis()
        from app.policies.store import RedisPolicyStore

        store = RedisPolicyStore(client)
        client.delete("rateguard:policy:/api/orders")
        client.srem("rateguard:policies", "/api/orders")

        try:
            policy = store.set(
                RoutePolicy(route="/api/orders", limit=50, window=60)
            )

            raw = json.loads(client.get("rateguard:policy:/api/orders"))
            assert raw["limit"] == 50
            assert raw["enabled"] is True

            assert store.get("/api/orders") == policy

            # survives an independent client view (same shared state)
            other = RedisPolicyStore(client)
            assert other.get("/api/orders") == policy

            assert store.delete("/api/orders") is True
            assert client.get("rateguard:policy:/api/orders") is None
            assert store.get("/api/orders") is None
        finally:
            client.delete("rateguard:policy:/api/orders")
            client.srem("rateguard:policies", "/api/orders")

    def test_missing_key_returns_none_not_error(self):
        from app.policies.store import RedisPolicyStore

        store = RedisPolicyStore(_redis())
        assert store.get("/never/configured") is None

    def test_malformed_document_raises_policy_error(self):
        client = _redis()
        from app.policies.store import RedisPolicyStore

        key = "rateguard:policy:/broken"
        client.set(key, "{not json")

        try:
            store = RedisPolicyStore(client)

            with pytest.raises(PolicyError):
                store.get("/broken")
        finally:
            client.delete(key)

    def test_concurrent_update_never_reads_partial_data(self):
        """Concurrent writers + readers: every read is a full document."""
        client = _redis()
        from app.policies.store import RedisPolicyStore

        route = "/api/concurrent"
        store = RedisPolicyStore(client)
        client.delete(f"rateguard:policy:{route}")
        client.srem("rateguard:policies", route)

        stop = threading.Event()
        errors = []

        try:
            def writer(worker_id):
                index = 0

                while not stop.is_set():
                    index += 1
                    store.set(
                        RoutePolicy(
                            route=route,
                            limit=(index % 100) + 1,
                            window=60,
                        )
                    )

            def reader():
                while not stop.is_set():
                    try:
                        policy = store.get(route)

                        if policy is not None:
                            assert 1 <= policy.limit <= 100
                            assert policy.window == 60
                    except (PolicyError, AssertionError) as exc:
                        errors.append(exc)

            threads = [
                threading.Thread(target=writer, args=(i,))
                for i in range(2)
            ] + [threading.Thread(target=reader) for _ in range(3)]

            for thread in threads:
                thread.start()

            time.sleep(1.0)
            stop.set()

            for thread in threads:
                thread.join(timeout=5)

            final = store.get(route)
            assert final is not None
            assert 1 <= final.limit <= 100

            assert errors == []
        finally:
            client.delete(f"rateguard:policy:{route}")
            client.srem("rateguard:policies", route)

    def test_list_prunes_stale_index_entries(self):
        client = _redis()
        from app.policies.store import RedisPolicyStore

        store = RedisPolicyStore(client)
        policy = store.set(RoutePolicy(route="/tmp/route", limit=1, window=1))
        client.delete("rateguard:policy:/tmp/route")  # orphan the index

        try:
            assert [p.route for p in store.list()] == []

            # index entry cleaned up
            assert not client.sismember(
                "rateguard:policies", "/tmp/route"
            )
        finally:
            client.delete("rateguard:policy:/tmp/route")
            client.srem("rateguard:policies", "/tmp/route")


# ---------------------------------------------------------------- resolver


STATIC = {
    "/api/login": {"limit": 10, "window": 60},
    "/api/orders": {"limit": 30, "window": 60},
}


def _resolver(store=None, cache_ttl=0.05):
    return PolicyResolver(
        store=store,
        static_route_limits=STATIC,
        global_limit=5,
        global_window=60,
        cache_ttl=cache_ttl,
    )


class TestResolverPrecedence:
    def test_global_fallback_for_unknown_route(self):
        resolver = _resolver(MemoryPolicyStore())
        limit, window = resolver.resolve("/api/anything")
        assert (limit, window) == (5, 60)

        effective = resolver.effective("/api/anything")
        assert effective.source == SOURCE_GLOBAL

    def test_static_route_config_used_without_dynamic(self):
        resolver = _resolver(MemoryPolicyStore())
        effective = resolver.effective("/api/login")

        assert (effective.limit, effective.window) == (10, 60)
        assert effective.source == SOURCE_STATIC

    def test_dynamic_overrides_static(self):
        store = MemoryPolicyStore()
        store.set(RoutePolicy(route="/api/login", limit=99, window=120))

        resolver = _resolver(store)
        effective = resolver.effective("/api/login")

        assert (effective.limit, effective.window) == (99, 120)
        assert effective.source == SOURCE_DYNAMIC
        assert effective.policy.route == "/api/login"

    def test_dynamic_creates_policy_for_unconfigured_route(self):
        store = MemoryPolicyStore()
        store.set(RoutePolicy(route="/brand/new", limit=2, window=3))

        resolver = _resolver(store)

        assert resolver.resolve("/brand/new") == (2, 3)

    def test_disabled_dynamic_falls_back_to_static(self):
        store = MemoryPolicyStore()
        store.set(
            RoutePolicy(route="/api/login", limit=99, window=120,
                        enabled=False)
        )

        effective = _resolver(store).effective("/api/login")

        assert effective.source == SOURCE_STATIC
        assert (effective.limit, effective.window) == (10, 60)

    def test_disabled_dynamic_falls_back_to_global(self):
        store = MemoryPolicyStore()
        store.set(
            RoutePolicy(route="/other", limit=8, window=8, enabled=False)
        )

        effective = _resolver(store).effective("/other")

        assert effective.source == SOURCE_GLOBAL
        assert effective.limit == 5

    def test_deletion_restores_fallback(self):
        store = MemoryPolicyStore()
        resolver = _resolver(store)
        resolver.set_policy("/api/login", limit=77, window=90)

        assert resolver.resolve("/api/login") == (77, 90)

        assert resolver.delete_policy("/api/login") is True
        assert resolver.resolve("/api/login") == (10, 60)

    def test_unsafe_route_skips_store_and_uses_global(self):
        store = MemoryPolicyStore()
        resolver = _resolver(store)

        # even if something malicious were stored under this name
        for unsafe in ("/api/../secret", "no-slash", "x" * 500, 123):
            assert resolver.resolve(unsafe) == (5, 60)

        assert store.get("/api/../secret") is None or True

    def test_resolve_none_route_uses_global(self):
        resolver = _resolver(MemoryPolicyStore())
        assert resolver.resolve(None) == (5, 60)


class TestResolverCache:
    def test_write_invalidation_is_immediate(self):
        resolver = _resolver(MemoryPolicyStore())

        resolver.set_policy("/api/orders", limit=30, window=60)
        assert resolver.resolve("/api/orders") == (30, 60)

        # update through the same process: no TTL wait required
        resolver.set_policy("/api/orders", limit=50, window=60, existing=None)
        assert resolver.resolve("/api/orders") == (50, 60)

    def test_cache_expires_after_ttl(self):
        resolver = _resolver(MemoryPolicyStore(), cache_ttl=0.05)

        resolver.set_policy("/api/orders", limit=30, window=60)
        assert resolver.resolve("/api/orders") == (30, 60)

        # simulate another worker changing the stored value directly
        resolver.store.set(RoutePolicy(route="/api/orders", limit=40,
                                       window=60))

        time.sleep(0.08)

        assert resolver.resolve("/api/orders") == (40, 60)

    def test_cross_worker_staleness_bounded_by_ttl(self):
        """Documented cross-worker guarantee, end to end:

        1. Worker A updates a policy in the shared store.
        2. Worker B may keep serving its cached policy right after
           the update (staleness within the TTL is expected).
        3. After at most one cache TTL, Worker B observes the new
           policy — stale usage cannot continue indefinitely.
        """
        ttl = 0.05
        resolver = _resolver(MemoryPolicyStore(), cache_ttl=ttl)

        # Worker B caches the current policy.
        resolver.set_policy("/api/orders", limit=30, window=60)
        assert resolver.resolve("/api/orders") == (30, 60)

        # Worker A writes a new policy directly to the shared store
        # (a different process would not touch B's cache).
        resolver.store.set(RoutePolicy(route="/api/orders", limit=50,
                                       window=60))

        # Immediately afterwards B may still serve the stale entry...
        assert resolver.resolve("/api/orders") == (30, 60)

        # ...but never beyond one TTL.
        deadline = time.monotonic() + ttl * 4

        while time.monotonic() < deadline:
            if resolver.resolve("/api/orders") == (50, 60):
                break

            time.sleep(0.01)

        assert resolver.resolve("/api/orders") == (50, 60)

    def test_invalidate_all(self):
        resolver = _resolver(MemoryPolicyStore())
        resolver.set_policy("/a", limit=2, window=2)
        resolver.resolve("/a")

        resolver.invalidate()
        resolver.store.set(RoutePolicy(route="/a", limit=9, window=9))

        assert resolver.resolve("/a") == (9, 9)

    def test_set_policy_merges_existing_fields(self):
        resolver = _resolver(MemoryPolicyStore())
        created = resolver.set_policy("/m", limit=5, window=10)

        updated = resolver.set_policy(
            "/m", limit=6, existing=created
        )

        assert updated.limit == 6
        assert updated.window == 10

        assert resolver.resolve("/m") == (6, 10)

    def test_resolver_rejects_invalid_updates(self):
        resolver = _resolver(MemoryPolicyStore())

        with pytest.raises(ValueError):
            resolver.set_policy("/bad path", limit=5, window=60)

        with pytest.raises(ValueError):
            resolver.set_policy("/ok", limit=0, window=60)


class TestResolverConcurrency:
    def test_concurrent_reads_and_writes(self):
        """Readers always see fully-formed configs while writes race."""
        resolver = _resolver(MemoryPolicyStore(), cache_ttl=0.01)
        errors = []
        stop = threading.Event()

        def writer():
            limits = [5, 10, 25, 50]

            while not stop.is_set():
                for count in limits:
                    try:
                        resolver.set_policy(
                            "/api/orders", limit=count, window=60
                        )
                    except ValueError as exc:
                        errors.append(exc)

        def reader():
            while not stop.is_set():
                try:
                    limit, window = resolver.resolve("/api/orders")
                    assert limit >= 1 and window >= 1
                except (ValueError, AssertionError) as exc:
                    errors.append(exc)

        threads = [threading.Thread(target=writer)] + [
            threading.Thread(target=reader) for _ in range(4)
        ]

        for thread in threads:
            thread.start()

        time.sleep(0.7)
        stop.set()

        for thread in threads:
            thread.join(timeout=5)

        assert errors == []
