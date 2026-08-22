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

Audit-aware mutations
---------------------
When constructed with an ``audit`` collaborator (an audit store
exposing ``max_events``, see :mod:`app.policies.audit`), both stores
additionally offer ``set_with_audit`` / ``delete_with_audit``. These
persist the policy change **and** its audit event atomically:

* Redis: one Lua script reads the authoritative previous document,
  writes the new state and appends the stream entry in a single
  execution — a mutation can never succeed while its audit event is
  silently lost, and the recorded ``previous_policy`` is exactly the
  document that was replaced (even under concurrent writers).
* Memory: the same sequence runs under one process lock.

Neither store catches Redis/network exceptions; callers decide how to
fail (the admin API answers 503, the resolver fails closed).
"""

import dataclasses
import json
import threading

from app.policies.audit import (
    AUDIT_STREAM_KEY,
    PolicyAuditEvent,
    stream_fields,
)
from app.policies.model import RoutePolicy


POLICY_INDEX_KEY = "rateguard:policies"
POLICY_KEY_PREFIX = "rateguard:policy:"


def policy_key(route: str) -> str:
    return f"{POLICY_KEY_PREFIX}{route}"


class PolicyError(RuntimeError):
    """Raised when a stored policy document cannot be parsed."""


class MemoryPolicyStore:
    """Process-local policy store. See module docstring."""

    def __init__(self, audit=None):
        self._policies = {}
        self._lock = threading.Lock()
        self.audit = audit

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

    # ------------------------------------------- audited mutations

    def _require_audit(self):
        if self.audit is None:
            raise TypeError(
                "set_with_audit/delete_with_audit require an audit "
                "store collaborator"
            )

    def set_with_audit(
        self, policy: RoutePolicy, event: PolicyAuditEvent
    ) -> RoutePolicy:
        """Persist the policy and its audit event under one lock.

        The recorded ``previous_policy`` snapshot is the document that
        was actually replaced, mirroring the Redis behaviour. The audit
        entry is appended **before** the policy commits: if recording
        fails, the mutation aborts with no state change (fail closed).
        """
        self._require_audit()

        with self._lock:
            previous = self._policies.get(policy.route)
            recorded = dataclasses.replace(
                event,
                previous_policy=(
                    previous.to_dict() if previous is not None else None
                ),
            )
            self.audit.append(recorded)
            self._policies[policy.route] = policy

        return policy

    def delete_with_audit(
        self, route: str, event: PolicyAuditEvent
    ) -> bool:
        """Delete the policy and record the event; False when absent.

        Nothing — not the deletion, not the event — happens when no
        policy exists for ``route``, or when recording fails.
        """
        self._require_audit()

        with self._lock:
            previous = self._policies.get(route)

            if previous is None:
                return False

            self.audit.append(
                dataclasses.replace(
                    event, previous_policy=previous.to_dict()
                )
            )
            del self._policies[route]

        return True


class RedisPolicyStore:
    """Redis-backed policy store shared by every worker and host."""

    # Read the authoritative previous document, write the new state and
    # append the audit entry in one atomic execution. ARGV:
    # 1 route, 2 new document JSON, 3 event_id, 4 timestamp (ISO),
    # 5 operation, 6 actor, 7 audit max events.
    _SET_WITH_AUDIT = """
local previous = redis.call('GET', KEYS[1])
redis.call('SET', KEYS[1], ARGV[2])
redis.call('SADD', KEYS[2], ARGV[1])
redis.call('XADD', KEYS[3], '*',
           'event_id', ARGV[3],
           'timestamp', ARGV[4],
           'operation', ARGV[5],
           'route', ARGV[1],
           'actor', ARGV[6],
           'previous_policy', previous or '',
           'new_policy', ARGV[2])
redis.call('XTRIM', KEYS[3], 'MAXLEN', tonumber(ARGV[7]))
return 1
"""

    # Delete only when a policy exists; record its actual document.
    _DELETE_WITH_AUDIT = """
local previous = redis.call('GET', KEYS[1])
if not previous then
    return 0
end
redis.call('DEL', KEYS[1])
redis.call('SREM', KEYS[2], ARGV[1])
redis.call('XADD', KEYS[3], '*',
           'event_id', ARGV[3],
           'timestamp', ARGV[4],
           'operation', ARGV[5],
           'route', ARGV[1],
           'actor', ARGV[6],
           'previous_policy', previous,
           'new_policy', '')
redis.call('XTRIM', KEYS[3], 'MAXLEN', tonumber(ARGV[7]))
return 1
"""

    def __init__(self, redis_client, audit=None):
        self.redis_client = redis_client
        self.audit = audit
        self._set_with_audit_script = self.redis_client.register_script(
            self._SET_WITH_AUDIT
        )
        self._delete_with_audit_script = self.redis_client.register_script(
            self._DELETE_WITH_AUDIT
        )

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

    # ------------------------------------------- audited mutations

    def _require_audit(self):
        if self.audit is None:
            raise TypeError(
                "set_with_audit/delete_with_audit require an audit "
                "store collaborator"
            )

    @staticmethod
    def _audit_argv(event: PolicyAuditEvent):
        """Event fields shared by both scripts (ARGV 3..6)."""
        fields = stream_fields(event)

        return [
            fields["timestamp"],
            fields["operation"],
            fields["actor"],
        ]

    def _script_keys(self, route: str):
        return [
            policy_key(route),
            POLICY_INDEX_KEY,
            AUDIT_STREAM_KEY,
        ]

    def set_with_audit(
        self, policy: RoutePolicy, event: PolicyAuditEvent
    ) -> RoutePolicy:
        """Atomically write the policy and its audit entry (Lua).

        The audit entry's ``previous_policy`` is read inside the script,
        so it always reflects the document that was actually replaced.
        """
        self._require_audit()

        document = json.dumps(policy.to_dict())

        self._set_with_audit_script(
            keys=self._script_keys(policy.route),
            args=[policy.route, document, event.event_id]
                 + self._audit_argv(event)
                 + [self.audit.max_events],
        )

        return policy

    def delete_with_audit(
        self, route: str, event: PolicyAuditEvent
    ) -> bool:
        """Atomically delete the policy and record the event.

        Returns False — writing nothing at all — when no policy exists.
        """
        self._require_audit()

        deleted = self._delete_with_audit_script(
            keys=self._script_keys(route),
            args=[route, "", event.event_id]
                 + self._audit_argv(event)
                 + [self.audit.max_events],
        )

        return bool(deleted)
