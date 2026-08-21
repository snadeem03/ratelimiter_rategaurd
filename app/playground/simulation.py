"""Playground simulation sessions.

The playground never re-implements rate limiting. Each simulation session
drives the REAL RateGuard algorithm implementations (in-memory or
Redis-backed) through ``create_rate_limiter`` and surfaces live state so the
browser can visualise the exact semantics the running API enforces.
"""

import threading
import time
import uuid
from collections import deque

import redis as redis_lib

from app.algorithms.factory import create_rate_limiter
from app.core.redis_client import get_redis
from app.storage.redis_storage import RedisStorage

SESSION_TTL_SECONDS = 1800.0
MAX_SESSIONS = 100
MAX_EVENTS = 500
MAX_BURST = 500
MAX_TIMESTAMPS = 120

ALGORITHMS = (
    "fixed_window",
    "sliding_window",
    "token_bucket",
    "leaky_bucket",
)

BACKENDS = ("memory", "redis")

_REDIS_KEY_SUFFIXES = {
    "fixed_window": (""),
    "sliding_window": ("", ":seq"),
    "token_bucket": (""),
    "leaky_bucket": ("", ":seq", ":last_leak"),
}


class RedisUnavailable(Exception):
    """Raised when a Redis-backed simulation cannot run.

    There is never a silent fallback to the memory implementation.
    """


def _now_ms():
    return int(time.time() * 1000)


def _parse_member(member):
    try:
        return float(member.split(":")[0])
    except (ValueError, AttributeError):
        return None


class SimSession:
    """One playground simulation session backed by a real limiter."""

    def __init__(
        self,
        session_id,
        algorithm,
        limit,
        window,
        backend,
        client_id,
        route,
        storage=None,
    ):
        self.session_id = session_id
        self.algorithm = algorithm
        self.limit = int(limit)
        self.window = int(window)
        self.backend = backend
        self.client_id = client_id
        self.route = route
        self.storage = storage

        self.events = deque(maxlen=MAX_EVENTS)
        self.requests = 0
        self.allowed = 0
        self.rejected = 0
        self.started = time.monotonic()
        self.last_access = time.monotonic()

        # Redis keys stay unique per session, route and client so a session
        # never observes another session's state.
        self.client_key = (
            f"playground:sim:{session_id}:{route}:{client_id}"
        )

        self.limiter = self._build_limiter()

    def _build_limiter(self):
        if self.backend == "redis":
            return create_rate_limiter(
                algorithm=self.algorithm,
                limit=self.limit,
                window=self.window,
                storage=self.storage,
                client_id=self.client_key,
            )

        return create_rate_limiter(
            algorithm=self.algorithm,
            limit=self.limit,
            window=self.window,
        )

    def touch(self):
        self.last_access = time.monotonic()

    def reset(self):
        """Recreate the limiter and drop recorded metrics/events."""
        if self.backend == "redis":
            try:
                self._delete_redis_keys()
            except redis_lib.RedisError as exc:
                raise RedisUnavailable(
                    "Redis became unavailable during the simulation"
                ) from exc

        self.events.clear()
        self.requests = 0
        self.allowed = 0
        self.rejected = 0
        self.started = time.monotonic()
        self.last_access = time.monotonic()
        self.limiter = self._build_limiter()

    def close(self):
        if self.backend == "redis":
            try:
                self._delete_redis_keys()
            except redis_lib.RedisError:
                pass

    def _delete_redis_keys(self):
        for suffix in _REDIS_KEY_SUFFIXES[self.algorithm]:
            full = f"rateguard:{self.algorithm}:{self.client_key}{suffix}"
            self.storage.client.delete(full)

    def send(self, count):
        self.touch()
        events = []

        for _ in range(count):
            try:
                allowed = bool(self.limiter.allow_request())
                remaining = int(self.limiter.remaining_requests())
                reset = int(self.limiter.reset_time())
            except redis_lib.RedisError as exc:
                raise RedisUnavailable(
                    "Redis became unavailable during the simulation"
                ) from exc

            if allowed:
                self.allowed += 1
            else:
                self.rejected += 1

            self.requests += 1

            event = {
                "ts": _now_ms(),
                "allowed": allowed,
                "status": 200 if allowed else 429,
                "remaining": remaining,
                "reset": reset,
                "route": self.route,
                "client": self.client_id,
            }

            events.append(event)
            self.events.append(event)

        return {
            "events": events,
            "state": self.snapshot(),
            "metrics": self.metrics(),
        }

    def metrics(self):
        elapsed = time.monotonic() - self.started
        rate = self.requests / elapsed if elapsed > 0 else 0.0
        remaining = int(self.limiter.remaining_requests())
        reset = int(self.limiter.reset_time())

        return {
            "requests": self.requests,
            "allowed": self.allowed,
            "rejected": self.rejected,
            "remaining": remaining,
            "limit": self.limit,
            "reset": reset,
            "elapsed": elapsed,
            "rate": rate,
            "success_pct": (
                100.0 * self.allowed / self.requests if self.requests else 0.0
            ),
        }

    def snapshot(self):
        base = {
            "algorithm": self.algorithm,
            "backend": self.backend,
            "limit": self.limit,
            "window": self.window,
            "remaining": int(self.limiter.remaining_requests()),
            "reset": int(self.limiter.reset_time()),
        }

        if self.backend == "redis":
            base.update(_redis_snapshot(self.limiter, self.algorithm))
        else:
            base.update(_memory_snapshot(self.limiter, self.algorithm))

        return base


def _memory_snapshot(limiter, algorithm):
    now = time.monotonic()

    if algorithm == "fixed_window":
        remaining = limiter.remaining_requests()
        reset = limiter.reset_time()
        window_elapsed = max(0.0, float(limiter.window - reset))

        return {
            "used": max(0, int(limiter.limit - remaining)),
            "window_elapsed": window_elapsed,
            "window_start": now - window_elapsed,
            "now": now,
        }

    if algorithm == "sliding_window":
        timestamps = [float(t) for t in limiter.requests]
        timestamps = timestamps[-MAX_TIMESTAMPS:]

        return {"timestamps": timestamps, "now": now}

    if algorithm == "token_bucket":
        return {
            "tokens": float(limiter.tokens),
            "capacity": int(limiter.capacity),
            "refill_rate": float(limiter.refill_rate),
            "last_refill": float(limiter.last_refill_time),
            "now": now,
        }

    if algorithm == "leaky_bucket":
        timestamps = [float(t) for t in limiter.queue]
        timestamps = timestamps[-MAX_TIMESTAMPS:]

        return {
            "timestamps": timestamps,
            "capacity": int(limiter.capacity),
            "leak_rate": float(limiter.leak_rate),
            "last_leak": float(limiter.last_leak_time),
            "now": now,
        }

    return {}


def _redis_snapshot(limiter, algorithm):
    client = limiter.redis_client

    def server_now():
        sec, micro = client.time()
        return float(sec) + float(micro) / 1_000_000.0

    now = server_now()

    if algorithm == "fixed_window":
        remaining = int(limiter.remaining_requests())
        reset = int(limiter.reset_time())
        used = max(0, int(limiter.limit) - remaining)
        window_elapsed = max(0.0, float(limiter.window - reset))

        return {
            "used": used,
            "window_elapsed": window_elapsed,
            "window_start": now - window_elapsed,
            "now": now,
        }

    if algorithm == "sliding_window":
        members = client.zrange(limiter.key, 0, -1)
        timestamps = [
            ts for ts in (_parse_member(m) for m in members) if ts is not None
        ]
        timestamps = timestamps[-MAX_TIMESTAMPS:]

        return {"timestamps": timestamps, "now": now}

    if algorithm == "token_bucket":
        raw = client.hget(limiter.key, "tokens")
        tokens = float(raw) if raw is not None else float(limiter.capacity)

        return {
            "tokens": tokens,
            "capacity": int(limiter.capacity),
            "refill_rate": float(limiter.refill_rate),
            "last_refill": now,
            "now": now,
        }

    if algorithm == "leaky_bucket":
        members = client.zrange(limiter.key, 0, -1)
        timestamps = [
            ts for ts in (_parse_member(m) for m in members) if ts is not None
        ]
        timestamps = timestamps[-MAX_TIMESTAMPS:]

        return {
            "timestamps": timestamps,
            "capacity": int(limiter.capacity),
            "leak_rate": float(limiter.leak_rate),
            "last_leak": now,
            "now": now,
        }

    return {}


_SESSIONS = {}
_SESSIONS_LOCK = threading.Lock()


def redis_available():
    try:
        return bool(get_redis().ping())
    except redis_lib.RedisError:
        return False


def create_session(
    algorithm,
    limit,
    window,
    backend="memory",
    client_id="client-1",
    route="/api/test",
):
    if algorithm not in ALGORITHMS:
        raise ValueError(f"Unknown algorithm: {algorithm}")

    if backend not in BACKENDS:
        raise ValueError(f"Unknown backend: {backend}")

    if int(limit) < 1 or int(window) < 1:
        raise ValueError("limit and window must be >= 1")

    storage = None

    if backend == "redis":
        if not redis_available():
            raise RedisUnavailable("Redis is unavailable")

        storage = RedisStorage(get_redis())

    session_id = uuid.uuid4().hex

    with _SESSIONS_LOCK:
        _evict_stale()

        # Hard cap so unauthenticated session creation cannot grow the
        # registry (and its Redis keys) without bound; the least recently
        # accessed session is closed first.
        while len(_SESSIONS) >= MAX_SESSIONS:
            oldest = min(
                _SESSIONS.values(),
                key=lambda session: session.last_access,
            )
            _SESSIONS.pop(oldest.session_id, None)
            oldest.close()

        session = SimSession(
            session_id=session_id,
            algorithm=algorithm,
            limit=limit,
            window=window,
            backend=backend,
            client_id=client_id,
            route=route,
            storage=storage,
        )

        _SESSIONS[session_id] = session

        return session


def get_session(session_id):
    with _SESSIONS_LOCK:
        session = _SESSIONS.get(session_id)

        if session is not None:
            session.touch()

        return session


def close_session(session_id):
    with _SESSIONS_LOCK:
        session = _SESSIONS.pop(session_id, None)

    if session is not None:
        session.close()


def session_payload(session):
    """State + metrics for a session, converting Redis outages into a
    ``RedisUnavailable`` error instead of a silent 500."""
    try:
        return {
            "state": session.snapshot(),
            "metrics": session.metrics(),
        }
    except redis_lib.RedisError as exc:
        raise RedisUnavailable("Redis is unavailable") from exc


def _evict_stale():
    now = time.monotonic()

    stale = [
        sid for sid, session in _SESSIONS.items()
        if now - session.last_access > SESSION_TTL_SECONDS
    ]

    for sid in stale:
        session = _SESSIONS.pop(sid, None)

        if session is not None:
            session.close()