"""Single-scenario benchmark execution against real RateGuard limiters."""

import threading
import time
from concurrent.futures import ThreadPoolExecutor

from app.algorithms.factory import create_rate_limiter
from benchmarks.metrics import latency_summary_ms

SUPPORTED_BACKENDS = ["memory", "redis"]
DEFAULT_LIMIT = 100
DEFAULT_WINDOW = 60


def build_limiter(algorithm: str, limit: int = DEFAULT_LIMIT,
                  window: int = DEFAULT_WINDOW):
    return create_rate_limiter(algorithm, limit=limit, window=window)


def execute_requests(limiter, requests: int, concurrency: int) -> dict:
    samples_ms = []
    allowed = 0
    rejected = 0
    lock = threading.Lock()

    def one_request(_index: int) -> float:
        nonlocal allowed, rejected
        start = time.perf_counter()
        is_allowed = limiter.allow_request()
        elapsed_ms = (time.perf_counter() - start) * 1000.0
        with lock:
            samples_ms.append(elapsed_ms)
            if is_allowed:
                allowed += 1
            else:
                rejected += 1
        return elapsed_ms

    started = time.perf_counter()
    with ThreadPoolExecutor(max_workers=concurrency) as executor:
        list(executor.map(one_request, range(requests)))
    elapsed_s = time.perf_counter() - started

    summary = latency_summary_ms(samples_ms)
    return {
        "requests": requests,
        "concurrency": concurrency,
        "elapsed_s": elapsed_s,
        "allowed": allowed,
        "rejected": rejected,
        "throughput_rps": requests / elapsed_s if elapsed_s > 0 else 0.0,
        **summary,
    }


def run_scenario(backend: str, algorithm: str, requests: int,
                 concurrency: int) -> dict:
    if backend == "redis":
        from benchmarks.redis_backend import run_redis_scenario

        return run_redis_scenario(
            algorithm=algorithm,
            requests=requests,
            concurrency=concurrency,
        )
    if backend != "memory":
        raise NotImplementedError(
            f"{backend} benchmarking is not implemented yet"
        )
    result = execute_requests(build_limiter(algorithm), requests, concurrency)
    return {
        "backend": backend,
        "algorithm": algorithm,
        **result,
    }
