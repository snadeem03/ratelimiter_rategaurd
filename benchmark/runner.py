"""Benchmark orchestration: build limiters, verify correctness, run,
clean up Redis state, and merge results."""

import time
import uuid

from app.algorithms.factory import create_rate_limiter
from app.storage.redis_storage import RedisStorage

from benchmark.config import BenchmarkConfig
from benchmark.metrics import summarize
from benchmark.redis import build_redis_client, redis_available
from benchmark.traffic_patterns import concurrent_traffic


def verify_limiter(make_limiter, limit: int) -> bool:
    """Sanity-check a fresh limiter enforces its configured limit.

    Runs before any timed measurement so the benchmark never optimizes
    past the actual limiter behaviour.
    """
    limiter = make_limiter()

    for _ in range(limit):
        if not limiter.allow_request():
            raise RuntimeError(
                f"Limiter rejected a request within its configured "
                f"limit of {limit}"
            )

    for _ in range(limit):
        if limiter.allow_request():
            raise RuntimeError(
                "Limiter allowed requests beyond its configured limit"
            )

    return True


def resolve_redis(config: BenchmarkConfig, client=None):
    """Decide whether Redis is usable for this configuration.

    - ``backend`` not redis/both -> (None, None)
    - Redis reachable             -> (RedisStorage, client)
    - ``backend=redis`` + down    -> raise RuntimeError (fail clearly)
    - ``backend=both`` + down     -> skip Redis, return (None, None)

    The caller prints an explicit skip message for the ``both`` case.
    """
    if config.backend not in ("redis", "both"):
        return None, None

    client = client or build_redis_client(config.redis_url)

    if redis_available(client):
        return RedisStorage(client), client

    if config.backend == "redis":
        raise RuntimeError(
            "Redis is unavailable: benchmark requested backend=redis but "
            "no Redis server is reachable. Check REDIS_URL and that "
            "Redis is running."
        )

    return None, None


def _cleanup_redis(redis_client, algorithm: str, run_id: str) -> None:
    """Delete every key created for this run so no state leaks between
    benchmark scenarios."""
    pattern = f"rateguard:{algorithm}:{run_id}:*"

    for key in redis_client.scan_iter(match=pattern, count=500):
        redis_client.delete(key)


def _interval_for(
    traffic: str,
    limit: int,
    window: int,
    configured: float | None,
) -> float:
    """Seconds between requests for paced traffic patterns.

    - burst     -> 0 (no pacing)
    - normal    -> 2x the sustainable spacing (well under the limit)
    - sustained -> 1x the sustainable spacing (paced at the limit)
    """
    if configured is not None:
        return configured

    if traffic == "burst":
        return 0.0

    spacing = window / limit if limit > 0 else 1.0

    if traffic == "normal":
        return spacing * 2

    return spacing


def run_scenario(
    config: BenchmarkConfig,
    algorithm: str,
    traffic: str,
    concurrency: int,
    storage=None,
    redis_client=None,
) -> dict:
    """Run one benchmark scenario and return a comparable result row."""
    backend = "redis" if storage is not None else "memory"

    run_id = f"bench:{uuid.uuid4().hex[:8]}"
    client_id = f"{run_id}:{algorithm}:{traffic}:c{concurrency}"
    verify_id = f"{run_id}:verify"

    def build(cid: str):
        if storage is not None:
            return create_rate_limiter(
                algorithm=algorithm,
                limit=config.limit,
                window=config.window,
                storage=storage,
                client_id=cid,
            )
        return create_rate_limiter(
            algorithm=algorithm,
            limit=config.limit,
            window=config.window,
        )

    if storage is not None:
        make_limiter = lambda _index: build(client_id)
        verify = lambda: build(verify_id)
    else:
        run_shared = build(None)
        verify_shared = build(None)
        make_limiter = lambda _index: run_shared
        verify = lambda: verify_shared

    verify_limiter(verify, config.limit)

    interval = _interval_for(traffic, config.limit, config.window, config.interval)

    start = time.perf_counter()
    summaries = concurrent_traffic(
        make_limiter,
        traffic,
        config.requests,
        concurrency,
        interval,
    )
    elapsed = time.perf_counter() - start

    allowed = sum(item[0] for item in summaries)
    rejected = sum(item[1] for item in summaries)
    latencies = [ms for item in summaries for ms in item[2]]

    if traffic == "burst" and allowed > config.limit:
        raise RuntimeError(
            f"Burst benchmark violated the configured rate limit: "
            f"allowed={allowed}, limit={config.limit}"
        )

    if redis_client is not None:
        _cleanup_redis(redis_client, algorithm, run_id)

    result = summarize(allowed, rejected, latencies, elapsed)
    result.update(
        {
            "algorithm": algorithm,
            "backend": backend,
            "traffic": traffic,
            "concurrency": concurrency,
        }
    )

    return result


def run_benchmarks(config: BenchmarkConfig) -> list[dict]:
    """Expand the config into every scenario and run them all."""
    storage, redis_client = resolve_redis(config)

    if config.backend == "both" and storage is None:
        print(
            "Redis is unavailable - skipping Redis benchmarks "
            "(running memory-only)."
        )
        backends = ["memory"]
    else:
        backends = config.backends()

    results = []

    for algorithm in config.algorithms():
        for traffic in config.traffic_patterns():
            for concurrency in config.concurrencies():
                for backend in backends:
                    scenario_storage = storage if backend == "redis" else None
                    scenario_client = redis_client if backend == "redis" else None

                    results.append(
                        run_scenario(
                            config,
                            algorithm,
                            traffic,
                            concurrency,
                            storage=scenario_storage,
                            redis_client=scenario_client,
                        )
                    )

    return results