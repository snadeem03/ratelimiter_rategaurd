"""Redis-backed benchmark execution.

Uses RateGuard's existing Redis architecture: the shared client from
``app.core.redis_client`` and the real Redis algorithm implementations via
the factory. No Redis code is reimplemented and nothing is mocked.

Every run gets a unique key namespace ``rateguard:{algorithm}:bench:{run_id}``
so concurrent runs never share state. Only keys created by the run are
deleted afterwards; Redis is never flushed and unrelated keys are never
touched.
"""

import uuid

from app.algorithms.factory import create_rate_limiter
from app.core.redis_client import get_redis
from app.storage.redis_storage import RedisStorage
from benchmarks.runner import (
    DEFAULT_LIMIT,
    DEFAULT_WINDOW,
    execute_requests,
)

BENCH_MARKER = "bench"


class RedisUnavailableError(RuntimeError):
    """Raised when a Redis benchmark is requested but Redis is unreachable."""


def redis_reachable(client) -> bool:
    try:
        return bool(client.ping())
    except Exception:
        return False


def require_redis(client=None):
    client = client or get_redis()
    if not redis_reachable(client):
        raise RedisUnavailableError(
            "Redis is not reachable; cannot run the requested "
            "Redis benchmark. Start Redis or check REDIS_URL."
        )
    return client


def new_run_id() -> str:
    return uuid.uuid4().hex[:12]


def benchmark_client_id(run_id: str) -> str:
    return f"{BENCH_MARKER}:{run_id}"


def run_key_pattern(run_id: str) -> str:
    return f"rateguard:*:{BENCH_MARKER}:{run_id}*"


def cleanup_run_keys(client, run_id: str) -> int:
    deleted = 0
    for key in client.scan_iter(run_key_pattern(run_id), count=500):
        client.delete(key)
        deleted += 1
    return deleted


def build_run_limiter(algorithm: str, client, run_id: str):
    return create_rate_limiter(
        algorithm,
        limit=DEFAULT_LIMIT,
        window=DEFAULT_WINDOW,
        storage=RedisStorage(client),
        client_id=benchmark_client_id(run_id),
    )


def run_redis_scenario(algorithm: str, requests: int, concurrency: int,
                       client=None, run_id: str = None) -> dict:
    run_id = run_id or new_run_id()
    client = require_redis(client)
    limiter = build_run_limiter(algorithm, client, run_id)
    limiter.allow_request()
    cleanup_run_keys(client, run_id)
    try:
        result = execute_requests(limiter, requests, concurrency)
    finally:
        cleanup_run_keys(client, run_id)
    return {
        "backend": "redis",
        "algorithm": algorithm,
        "run_id": run_id,
        **result,
    }
