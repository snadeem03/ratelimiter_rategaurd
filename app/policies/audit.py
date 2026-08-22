"""Append-only audit history for dynamic rate-limit policy changes.

This is an audit **trail**, not a logging system: exactly one immutable
event is recorded per successful policy mutation (create / update /
delete) made through the admin API. The event captures what changed,
when, and by whom — never secrets. Audit history is *never* read by the
rate-limit hot path; the current policy remains owned by the
``PolicyStore`` / ``PolicyResolver``.

There are no separate ``enable``/``disable`` operations: the policy
model has no dedicated endpoints, so toggling ``enabled`` is a plain
``update`` whose before/after snapshots make the change visible.

Event IDs are random UUIDs (hex) — unique, unguessable and free of any
secret material. Timestamps are timezone-aware UTC datetimes serialized
as ISO 8601 with an explicit offset, which also sorts correctly as a
plain string.

Stores
------
``MemoryAuditStore``
    A bounded ``collections.deque`` guarded by a lock. **Process-local,
    ephemeral, not shared between workers, lost on restart.** Memory
    mode does not provide distributed audit history — it exists for
    single-process use, testing, and parity with the memory backend.

``RedisAuditStore``
    A single Redis Stream (``rateguard:audit:policies``). Streams give
    append semantics (``XADD``), server-side chronological ordering of
    concurrent writers, and bounded retention (``XTRIM MAXLEN``) in one
    native structure — one key total, never one key per event. When the
    configured maximum is reached the oldest entries are discarded and
    the newest are retained.

Retention bound: exact ``MAXLEN`` trimming on every append keeps the
stream deterministically bounded (cheap amortized cost at the default
of 1000 events).

Neither store catches Redis/network exceptions; callers decide how to
fail (the admin API answers 503 and the mutation is rolled back with it
— see ``RedisPolicyStore.set_with_audit`` / ``delete_with_audit``).
"""

import dataclasses
import json
import threading
import uuid
from collections import deque
from datetime import datetime, timezone

from app.policies.model import validate_route


AUDIT_STREAM_KEY = "rateguard:audit:policies"

DEFAULT_ACTOR = "admin"

# Mutations that produce audit events. Reads are intentionally absent:
# this is a change trail, not an access log.
AUDIT_OPERATIONS = frozenset({"create", "update", "delete"})

# Serialized field names — exactly these, nothing else, ever.
EVENT_FIELDS = (
    "event_id",
    "timestamp",
    "operation",
    "route",
    "previous_policy",
    "new_policy",
    "actor",
)


class AuditError(ValueError):
    """Raised when an audit event would be malformed."""


def utc_now() -> datetime:
    """Timezone-aware UTC timestamp; immune to machine-local timezone."""
    return datetime.now(timezone.utc)


@dataclasses.dataclass(frozen=True)
class PolicyAuditEvent:
    """One immutable record of a dynamic policy change."""

    event_id: str
    timestamp: datetime
    operation: str
    route: str
    previous_policy: dict | None
    new_policy: dict | None
    actor: str = DEFAULT_ACTOR

    def __post_init__(self):
        if not isinstance(self.event_id, str) or not self.event_id:
            raise AuditError("event_id must be a non-empty string")

        if not isinstance(self.timestamp, datetime):
            raise AuditError(
                f"timestamp must be a datetime, got "
                f"{type(self.timestamp).__name__}"
            )

        if self.timestamp.tzinfo is None:
            raise AuditError(
                "timestamp must be timezone-aware (use audit.utc_now)"
            )

        if self.operation not in AUDIT_OPERATIONS:
            allowed = ", ".join(sorted(AUDIT_OPERATIONS))
            raise AuditError(
                f"operation must be one of {allowed}, "
                f"got {self.operation!r}"
            )

        try:
            object.__setattr__(self, "route", validate_route(self.route))
        except ValueError as exc:
            raise AuditError(str(exc)) from exc

        for name in ("previous_policy", "new_policy"):
            value = getattr(self, name)

            if value is not None and not isinstance(value, dict):
                raise AuditError(
                    f"{name} must be a mapping or null, got "
                    f"{type(value).__name__}"
                )

        if not isinstance(self.actor, str) or not self.actor:
            raise AuditError("actor must be a non-empty string")

    def to_dict(self) -> dict:
        """Plain JSON-safe representation (exactly EVENT_FIELDS)."""
        return {
            "event_id": self.event_id,
            "timestamp": self.timestamp.isoformat(),
            "operation": self.operation,
            "route": self.route,
            "previous_policy": (
                dict(self.previous_policy)
                if self.previous_policy is not None else None
            ),
            "new_policy": (
                dict(self.new_policy) if self.new_policy is not None
                else None
            ),
            "actor": self.actor,
        }

    @classmethod
    def from_dict(cls, data) -> "PolicyAuditEvent":
        """Rebuild an event from its serialized representation."""
        if not isinstance(data, dict):
            raise AuditError(
                f"event must be an object, got {type(data).__name__}"
            )

        missing = [f for f in EVENT_FIELDS if f not in data]

        if missing:
            raise AuditError(
                "event is missing required fields: " + ", ".join(missing)
            )

        try:
            timestamp = datetime.fromisoformat(data["timestamp"])
        except (TypeError, ValueError) as exc:
            raise AuditError(
                f"invalid timestamp: {data['timestamp']!r}"
            ) from exc

        return cls(
            event_id=data["event_id"],
            timestamp=timestamp,
            operation=data["operation"],
            route=data["route"],
            previous_policy=data["previous_policy"],
            new_policy=data["new_policy"],
            actor=data["actor"],
        )


def new_event(
    operation: str,
    route: str,
    previous_policy=None,
    new_policy=None,
    actor: str = DEFAULT_ACTOR,
    now: datetime | None = None,
) -> PolicyAuditEvent:
    """Create a fresh audit event with a random ID and UTC timestamp."""
    return PolicyAuditEvent(
        event_id=uuid.uuid4().hex,
        timestamp=now if now is not None else utc_now(),
        operation=operation,
        route=route,
        previous_policy=previous_policy,
        new_policy=new_policy,
        actor=actor,
    )


def stream_fields(event: PolicyAuditEvent) -> dict:
    """Flatten an event into Redis stream entry fields.

    Policy snapshots travel as JSON strings; nulls become empty strings
    because Redis does not store empty fields.
    """
    data = event.to_dict()

    return {
        "event_id": data["event_id"],
        "timestamp": data["timestamp"],
        "operation": data["operation"],
        "route": data["route"],
        "previous_policy": (
            _dumps(data["previous_policy"])
            if data["previous_policy"] is not None else ""
        ),
        "new_policy": (
            _dumps(data["new_policy"]) if data["new_policy"] is not None
            else ""
        ),
        "actor": data["actor"],
    }


def from_stream_fields(fields: dict) -> PolicyAuditEvent:
    """Rebuild an event from Redis stream entry fields."""
    raw = {
        "previous_policy": fields.get("previous_policy") or None,
        "new_policy": fields.get("new_policy") or None,
    }

    for name, value in list(raw.items()):
        if value is not None:
            try:
                raw[name] = json.loads(value)
            except (TypeError, ValueError) as exc:
                raise AuditError(
                    f"stored {name} snapshot is malformed"
                ) from exc

    return PolicyAuditEvent(
        event_id=fields["event_id"],
        timestamp=datetime.fromisoformat(fields["timestamp"]),
        operation=fields["operation"],
        route=fields["route"],
        previous_policy=raw["previous_policy"],
        new_policy=raw["new_policy"],
        actor=fields["actor"],
    )


def _dumps(snapshot: dict) -> str:
    return json.dumps(snapshot, separators=(",", ":"))


class MemoryAuditStore:
    """Bounded, process-local audit history. See module docstring."""

    def __init__(self, max_events: int = 1000):
        if int(max_events) < 1:
            raise ValueError("max_events must be >= 1")

        self.max_events = int(max_events)
        self._events: deque[PolicyAuditEvent] = deque(maxlen=self.max_events)
        self._lock = threading.Lock()

    def append(self, event: PolicyAuditEvent) -> PolicyAuditEvent:
        """Record one event; the oldest entry is dropped once full."""
        with self._lock:
            self._events.append(event)

        return event

    def list(
        self,
        limit: int | None = None,
        route: str | None = None,
        operation: str | None = None,
    ):
        """Newest-first listing with optional exact-match filters."""
        with self._lock:
            events = list(self._events)

        events.reverse()

        if route is not None:
            events = [e for e in events if e.route == route]

        if operation is not None:
            events = [e for e in events if e.operation == operation]

        if limit is not None:
            events = events[:max(0, int(limit))]

        return events

    def get(self, event_id: str):
        with self._lock:
            for event in reversed(self._events):
                if event.event_id == event_id:
                    return event

        return None

    def clear(self):
        with self._lock:
            self._events.clear()


class RedisAuditStore:
    """Redis Stream-backed audit history shared by every worker."""

    def __init__(self, redis_client, max_events: int = 1000):
        if int(max_events) < 1:
            raise ValueError("max_events must be >= 1")

        self.redis_client = redis_client
        self.max_events = int(max_events)

    def append(self, event: PolicyAuditEvent) -> PolicyAuditEvent:
        pipe = self.redis_client.pipeline(transaction=True)

        pipe.xadd(AUDIT_STREAM_KEY, stream_fields(event))
        # Exact trimming keeps the bound deterministic (the Lua-backed
        # combined writes trim exactly as well).
        pipe.xtrim(AUDIT_STREAM_KEY, maxlen=self.max_events,
                   approximate=False)

        pipe.execute()

        return event

    def list(
        self,
        limit: int | None = None,
        route: str | None = None,
        operation: str | None = None,
    ):
        """Newest-first listing with optional exact-match filters.

        At most ``scan_window`` recent entries are examined per call so
        filtered reads stay bounded even when filters match old events.
        """
        count = max(1, int(limit or 50))
        scan_window = min(max(count * 5, 100), 500)

        entries = self.redis_client.xrevrange(
            AUDIT_STREAM_KEY, count=scan_window
        )

        events = []

        for _entry_id, fields in entries:
            event = from_stream_fields(fields)

            if route is not None and event.route != route:
                continue

            if operation is not None and event.operation != operation:
                continue

            events.append(event)

            if len(events) >= count:
                break

        return events

    def trim(self):
        """Enforce the retention bound (used after out-of-band appends)."""
        self.redis_client.xtrim(
            AUDIT_STREAM_KEY, maxlen=self.max_events, approximate=False
        )
