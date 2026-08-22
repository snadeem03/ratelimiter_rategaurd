"""Runtime policy resolution with precedence and a small TTL cache.

Effective configuration for a route is resolved in this order:

1. **dynamic**  — a runtime-managed ``RoutePolicy`` (admin API), enabled
2. **static**   — the ``RATE_LIMIT_ROUTES`` environment configuration
3. **global**   — ``RATE_LIMIT`` / ``RATE_LIMIT_WINDOW`` defaults

A *disabled* dynamic policy does not block the chain: the route falls
back to static/global exactly as if no dynamic policy existed. Deleting
a dynamic policy therefore restores the configured fallback, never an
unlimited route.

The resolver caches effective results per route for a short TTL
(default 2 seconds) to keep the request hot path free of extra Redis
round-trips. Writes through :meth:`set_policy` / :meth:`delete_policy`
invalidate this process's cache immediately; other workers converge
within the TTL at most. Cached entries are only ever replaced wholesale
from a fully validated store read, so a concurrent update can never
surface partial policy data.
"""

import re
import threading
import time

from app.policies.model import (
    MAX_ROUTE_LENGTH,
    RoutePolicy,
    _ROUTE_RE,
)
from app.policies.store import MemoryPolicyStore


DEFAULT_CACHE_TTL = 2.0

SOURCE_DYNAMIC = "dynamic"
SOURCE_STATIC = "static"
SOURCE_GLOBAL = "global"


class EffectivePolicy:
    """The resolved rate-limit configuration for one route."""

    __slots__ = ("route", "limit", "window", "enabled", "source", "policy")

    def __init__(
        self,
        route,
        limit,
        window,
        source,
        enabled=True,
        policy=None,
    ):
        self.route = route
        self.limit = limit
        self.window = window
        self.enabled = enabled
        self.source = source
        self.policy = policy

    def to_dict(self) -> dict:
        return {
            "route": self.route,
            "limit": self.limit,
            "window": self.window,
            "enabled": self.enabled,
            "source": self.source,
        }


class PolicyResolver:
    """Resolve per-route limits: dynamic > static > global."""

    def __init__(
        self,
        store=None,
        static_route_limits=None,
        global_limit=5,
        global_window=60,
        cache_ttl=DEFAULT_CACHE_TTL,
    ):
        self.store = store if store is not None else MemoryPolicyStore()
        self._static = dict(static_route_limits or {})
        self.global_limit = global_limit
        self.global_window = global_window
        self.cache_ttl = float(cache_ttl)

        # route -> (expires_at_monotonic, EffectivePolicy)
        self._cache = {}
        self._lock = threading.Lock()

    # -------------------------------------------------- resolution

    def _lookup_dynamic(self, route):
        """Return the stored policy for ``route``, or None.

        Requests may carry arbitrary path strings; anything that could
        not be a stored policy (wrong type, overlong, outside the safe
        charset) skips the store entirely instead of producing keys or
        errors on the hot path.
        """
        if not isinstance(route, str):
            return None

        if len(route) > MAX_ROUTE_LENGTH or not _ROUTE_RE.match(route):
            return None

        try:
            return self.store.get(route)
        except Exception:
            # Fail closed: let storage outages surface loudly rather
            # than silently bypassing or freezing stale policies.
            raise

    def effective(self, route) -> EffectivePolicy:
        now = time.monotonic()

        with self._lock:
            cached = self._cache.get(route)

            if cached is not None and cached[0] > now:
                return cached[1]

        effective = self._resolve(route)

        expires_at = time.monotonic() + self.cache_ttl

        with self._lock:
            self._cache[route] = (expires_at, effective)

            # Keep the cache bounded even under pathological route
            # variety: entries expire via TTL, this caps worst case.
            while len(self._cache) > 10_000:
                self._cache.pop(next(iter(self._cache)))

        return effective

    def _resolve(self, route) -> EffectivePolicy:
        policy = self._lookup_dynamic(route)

        if policy is not None and policy.enabled:
            return EffectivePolicy(
                route=route,
                limit=policy.limit,
                window=policy.window,
                source=SOURCE_DYNAMIC,
                enabled=True,
                policy=policy,
            )

        static = self._static.get(route) if isinstance(route, str) else None

        if static is not None:
            return EffectivePolicy(
                route=route,
                limit=static["limit"],
                window=static["window"],
                source=SOURCE_STATIC,
                enabled=False if policy is not None else None,
                policy=policy,
            )

        return EffectivePolicy(
            route=route,
            limit=self.global_limit,
            window=self.global_window,
            source=SOURCE_GLOBAL,
            enabled=False if policy is not None else None,
            policy=policy,
        )

    def resolve(self, route):
        """Return ``(limit, window)`` enforced for ``route``."""
        effective = self.effective(route)
        return effective.limit, effective.window

    # -------------------------------------------------- management

    def set_policy(
        self,
        route,
        limit=None,
        window=None,
        enabled=True,
        existing=None,
    ) -> RoutePolicy:
        """Create or replace the policy for ``route`` atomically.

        When updating an ``existing`` policy, omitted fields inherit its
        values. The store write is atomic; this process's cache entry is
        dropped so subsequent requests immediately observe the change.
        """
        base = existing.to_dict() if existing else {
            "route": route,
            "limit": limit,
            "window": window,
        }

        data = {
            "route": base["route"],
            "limit": limit if limit is not None else base["limit"],
            "window": window if window is not None else base["window"],
            "enabled": enabled,
        }

        policy = RoutePolicy.from_dict(data)
        self.store.set(policy)
        self.invalidate(policy.route)

        return policy

    def delete_policy(self, route) -> bool:
        deleted = bool(self.store.delete(route))

        if deleted:
            self.invalidate(route)

        return deleted

    def list_policies(self):
        return self.store.list()

    def get_stored(self, route):
        try:
            return self._lookup_dynamic(route)
        except ValueError:
            return None

    def invalidate(self, route=None):
        """Drop cached resolutions (one route, or all)."""
        with self._lock:
            if route is None:
                self._cache.clear()
            else:
                self._cache.pop(route, None)


def is_safe_route(route) -> bool:
    """Fast check used by API layers before touching a store."""
    return isinstance(route, str) and bool(re.match(_ROUTE_RE, route))
