"""Policy stores for runtime-managed rate-limit configuration.

Two implementations share one interface:

``MemoryPolicyStore``
    A plain in-process dict guarded by a lock. Policies are
    **process-local**: with multiple uvicorn workers each worker keeps
    its own copy and updates are NOT visible to other workers or to a
    restarted process. Memory mode therefore does not provide
    distributed configuration — it exists for single-process use,
    testing, and parity with the ``memory`` rate-limit backend.

``RedisPolicyStore``
    One JSON document per route under ``rateguard:policy:{route}``
    plus an index set (``rateguard:policies``) for listing. Writes are
    single Redis ``SET`` operations, so every reader observes either
    the previous or the new document — never a partial one. All
    workers of all hosts share this state. No TTL: policies persist
    until explicitly deleted.

Neither store catches Redis/network exceptions; callers decide how to
fail (the admin API answers 503, the resolver fails closed).
"""

import json
import threading

from app.policies.model import RoutePolicy


POLICY_INDEX_KEY = "rateguard:policies"
POLICY_KEY_PREFIX = "rateguard:policy:"


def policy_key(route: str) -> str:
    return f"{POLICY_KEY_PREFIX}{route}"


class PolicyError(RuntimeError):
    """Raised when a stored policy document cannot be parsed."""


class MemoryPolicyStore:
    """Process-local policy store. See module docstring."""

    def __init__(self):
        self._policies = {}
        self._lock = threading.Lock()

    def set(self, policy: RoutePolicy) -> RoutePolicy:
        with self._lock:
            self._policies[policy.route] = policy

        return policy

    def get(self, route: str):
        with self._lock:
            return self._policies.get(route)

    def delete(self, route: str) -> bool:
        with self._lock:
            return self._policies.pop(route, None) is not None

    def list(self):
        with self._lock:
            return sorted(
                self._policies.values(), key=lambda p: p.route
            )

    def clear(self):
        with self._lock:
            self._policies.clear()


class RedisPolicyStore:
    """Redis-backed policy store shared by every worker and host."""

    def __init__(self, redis_client):
        self.redis_client = redis_client

    @staticmethod
    def _decode(route: str, raw) -> RoutePolicy:
        try:
            data = json.loads(raw)
        except (TypeError, ValueError) as exc:
            raise PolicyError(
                f"stored policy for {route!r} is malformed"
            ) from exc

        try:
            return RoutePolicy.from_dict(data)
        except ValueError as exc:
            raise PolicyError(
                f"stored policy for {route!r} is invalid: {exc}"
            ) from exc

    def set(self, policy: RoutePolicy) -> RoutePolicy:
        pipe = self.redis_client.pipeline(transaction=True)

        pipe.set(policy_key(policy.route), json.dumps(policy.to_dict()))
        pipe.sadd(POLICY_INDEX_KEY, policy.route)
        pipe.execute()

        return policy

    def get(self, route: str):
        raw = self.redis_client.get(policy_key(route))

        if raw is None:
            return None

        return self._decode(route, raw)

    def delete(self, route: str) -> bool:
        pipe = self.redis_client.pipeline(transaction=True)

        pipe.delete(policy_key(route))
        pipe.srem(POLICY_INDEX_KEY, route)

        return bool(pipe.execute()[0])

    def list(self):
        routes = sorted(self.redis_client.smembers(POLICY_INDEX_KEY))
        policies = []
        stale = []

        pipe = self.redis_client.pipeline()

        for route in routes:
            pipe.get(policy_key(route))

        for route, raw in zip(routes, pipe.execute()):
            if raw is None:
                stale.append(route)
                continue

            policies.append(self._decode(route, raw))

        if stale:
            self.redis_client.srem(POLICY_INDEX_KEY, *stale)

        return policies
