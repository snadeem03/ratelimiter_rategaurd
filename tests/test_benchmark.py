"""Tests for the RateGuard benchmark system.

Unit tests avoid depending on exact timing numbers or a live Redis server.
Redis-backed scenarios are skipped when no Redis is reachable.
"""

import json
import threading

import pytest

from benchmark.benchmark import build_parser, config_from_args, main
from benchmark.config import (
    ALGORITHMS,
    DEFAULT_CONCURRENCIES,
    TRAFFIC_PATTERNS,
    BenchmarkConfig,
)
from benchmark.metrics import percentile, summarize
from benchmark.redis import build_redis_client, redis_available
from benchmark.results import (
    FIELDS,
    save_results,
    to_csv,
    to_json,
    to_table,
)
from benchmark.runner import (
    resolve_redis,
    run_benchmarks,
    run_scenario,
    verify_limiter,
)
from benchmark.traffic_patterns import (
    TRAFFIC_FNS,
    TimedLimiter,
    burst_traffic,
    concurrent_traffic,
)

from app.algorithms.factory import create_rate_limiter

try:
    from app.core.redis_client import get_redis

    get_redis().ping()
    REDIS_AVAILABLE = True
except Exception:
    REDIS_AVAILABLE = False

REDIS_REASON = "Redis is not available"


class _CountingLimiter:
    """Deterministic fake limiter: allows exactly ``limit`` requests."""

    def __init__(self, limit=5):
        self.limit = limit
        self.count = 0
        self._lock = threading.Lock()

    def allow_request(self):
        with self._lock:
            if self.count < self.limit:
                self.count += 1
                return True
            return False


class _BrokenLimiter:
    """Fake limiter that never enforces its limit."""

    def allow_request(self):
        return True


# ---------------------------------------------------------------- config


def test_config_defaults():
    config = BenchmarkConfig()
    assert config.backend == "memory"
    assert config.algorithm == "all"
    assert config.traffic == "burst"
    assert config.requests == 1000
    assert config.concurrency == "1"
    assert config.limit == 100
    assert config.window == 60


def test_config_expansion_all():
    config = BenchmarkConfig(
        backend="both",
        algorithm="all",
        traffic="all",
        concurrency="all",
    )
    assert config.algorithms() == ALGORITHMS
    assert config.traffic_patterns() == TRAFFIC_PATTERNS
    assert config.concurrencies() == DEFAULT_CONCURRENCIES
    assert config.backends() == ["memory", "redis"]


def test_config_concurrency_list_and_all():
    config = BenchmarkConfig(concurrency="1,10,50")
    assert config.concurrencies() == [1, 10, 50]

    config = BenchmarkConfig(concurrency="all")
    assert config.concurrencies() == DEFAULT_CONCURRENCIES


def test_config_concurrency_invalid():
    with pytest.raises(ValueError):
        BenchmarkConfig(concurrency=",,,").concurrencies()


def test_config_algorithm_override():
    config = BenchmarkConfig(algorithm="token_bucket")
    assert config.algorithms() == ["token_bucket"]


def test_config_backends_both():
    config = BenchmarkConfig(backend="both")
    assert config.backends() == ["memory", "redis"]


# ---------------------------------------------------------------- metrics


def test_percentile_empty():
    assert percentile([], 95) == 0.0


def test_percentile_single():
    assert percentile([3.5], 95) == 3.5


def test_percentile_known_values():
    values = [1.0, 2.0, 3.0, 4.0, 5.0]
    assert percentile(values, 0) == 1.0
    assert percentile(values, 50) == 3.0
    assert percentile(values, 100) == 5.0
    assert 0.0 < percentile(values, 95) <= 5.0


def test_summarize_structure():
    result = summarize(allowed=5, rejected=5, latencies_ms=[1.0, 2.0, 3.0], elapsed=0.5)

    for key in (
        "requests",
        "allowed",
        "rejected",
        "elapsed",
        "rps",
        "avg_latency_ms",
        "p50_latency_ms",
        "p95_latency_ms",
        "p99_latency_ms",
    ):
        assert key in result

    assert result["requests"] == 10
    assert result["allowed"] == 5
    assert result["rejected"] == 5
    assert result["rps"] > 0


# ------------------------------------------------------- traffic patterns


def test_traffic_fns_expose_all_patterns():
    assert set(TRAFFIC_FNS) == set(TRAFFIC_PATTERNS)


def test_concurrent_traffic_unknown_pattern():
    with pytest.raises(KeyError):
        concurrent_traffic(lambda _i: _CountingLimiter(), "spike", 10, 1)


def test_concurrent_traffic_single_thread():
    summaries = concurrent_traffic(
        lambda _i: _CountingLimiter(limit=5),
        "burst",
        10,
        1,
    )
    allowed = sum(item[0] for item in summaries)
    rejected = sum(item[1] for item in summaries)
    latencies = [ms for item in summaries for ms in item[2]]

    assert allowed == 5
    assert rejected == 5
    assert len(latencies) == 10


def test_concurrent_traffic_multi_thread():
    shared = _CountingLimiter(limit=8)

    summaries = concurrent_traffic(
        lambda _i: shared,
        "burst",
        25,
        3,
    )
    allowed = sum(item[0] for item in summaries)
    rejected = sum(item[1] for item in summaries)

    assert allowed == 8
    assert rejected == 17
    assert len(summaries) == 3


def test_concurrent_traffic_rejects_invalid_concurrency():
    with pytest.raises(ValueError):
        concurrent_traffic(lambda _i: _CountingLimiter(), "burst", 10, 0)


def test_timed_limiter_records_metrics():
    timed = TimedLimiter(_CountingLimiter(limit=2))
    burst_traffic(timed, 5)

    assert timed.allowed == 2
    assert timed.rejected == 3
    assert len(timed.latencies_ms) == 5


# ---------------------------------------------------------------- results


def test_to_table_includes_headers():
    result = {
        "algorithm": "token_bucket",
        "backend": "memory",
        "traffic": "burst",
        "concurrency": 1,
        "requests": 10,
        "allowed": 5,
        "rejected": 5,
        "elapsed": 0.001,
        "rps": 10000.0,
        "avg_latency_ms": 0.1,
        "p50_latency_ms": 0.1,
        "p95_latency_ms": 0.2,
        "p99_latency_ms": 0.3,
    }

    table = to_table([result])

    assert "Algorithm" in table
    assert "Backend" in table
    assert "Traffic" in table
    assert "Concurrency" in table
    assert "RPS" in table
    assert "token_bucket" in table


def test_to_csv_round_trip():
    results = [
        {
            "algorithm": "fixed_window",
            "backend": "memory",
            "traffic": "burst",
            "concurrency": 1,
            "requests": 10,
            "allowed": 5,
            "rejected": 5,
            "elapsed": 0.001,
            "rps": 10000.0,
            "avg_latency_ms": 0.1,
            "p50_latency_ms": 0.1,
            "p95_latency_ms": 0.2,
            "p99_latency_ms": 0.3,
        }
    ]

    csv_text = to_csv(results)

    assert "algorithm,backend,traffic,concurrency" in csv_text
    assert "fixed_window,memory,burst,1" in csv_text


def test_to_json_round_trip():
    results = [{"algorithm": "leaky_bucket", "backend": "redis", "rps": 123.45}]

    parsed = json.loads(to_json(results))

    assert parsed == results


def test_save_results_writes_files(tmp_path):
    results = [
        {
            "algorithm": "sliding_window",
            "backend": "redis",
            "traffic": "burst",
            "concurrency": 10,
            "requests": 100,
            "allowed": 50,
            "rejected": 50,
            "elapsed": 0.01,
            "rps": 10000.0,
            "avg_latency_ms": 0.1,
            "p50_latency_ms": 0.1,
            "p95_latency_ms": 0.2,
            "p99_latency_ms": 0.3,
        }
    ]

    paths = save_results(results, str(tmp_path), fmt="all")

    assert "csv" in paths
    assert "json" in paths

    with open(paths["csv"], encoding="utf-8") as handle:
        assert "sliding_window" in handle.read()

    with open(paths["json"], encoding="utf-8") as handle:
        assert json.load(handle)[0]["algorithm"] == "sliding_window"


def test_result_fields_cover_table_columns():
    from benchmark.results import COLUMNS

    assert {key for _, key in COLUMNS} <= set(FIELDS)


# ---------------------------------------------------------------- cli


def test_parser_accepts_benchmark_flags():
    args = build_parser().parse_args(
        [
            "--backend",
            "redis",
            "--algorithm",
            "token_bucket",
            "--traffic",
            "burst",
            "--requests",
            "1000",
            "--concurrency",
            "1,10,50",
        ]
    )

    assert args.backend == "redis"
    assert args.algorithm == "token_bucket"
    assert args.traffic == "burst"
    assert args.requests == 1000
    assert args.concurrency == "1,10,50"


def test_config_from_args():
    args = build_parser().parse_args(
        [
            "--backend",
            "both",
            "--concurrency",
            "all",
            "--format",
            "json",
            "--redis-url",
            "redis://example:6379/1",
        ]
    )

    config = config_from_args(args)

    assert config.backend == "both"
    assert config.concurrencies() == DEFAULT_CONCURRENCIES
    assert config.format == "json"
    assert config.redis_url == "redis://example:6379/1"


def test_cli_main_memory_exit_zero(capsys):
    exit_code = main(
        [
            "--backend",
            "memory",
            "--traffic",
            "burst",
            "--requests",
            "10",
            "--limit",
            "5",
            "--format",
            "table",
        ]
    )

    captured = capsys.readouterr()

    assert exit_code == 0
    assert "Algorithm" in captured.out


def test_cli_main_redis_unavailable_fails():
    exit_code = main(
        [
            "--backend",
            "redis",
            "--redis-url",
            "redis://127.0.0.1:6399/0",
            "--traffic",
            "burst",
            "--requests",
            "5",
            "--limit",
            "5",
            "--format",
            "table",
        ]
    )

    assert exit_code == 1


# ------------------------------------------------------- redis availability


class _FailingPing:
    def ping(self):
        raise ConnectionError("boom")


class _WorkingPing:
    def ping(self):
        return True


def test_redis_available_true():
    assert redis_available(_WorkingPing()) is True


def test_redis_available_false():
    assert redis_available(_FailingPing()) is False


def test_resolve_redis_memory_ignores_client():
    storage, client = resolve_redis(BenchmarkConfig(backend="memory"), _FailingPing())
    assert storage is None
    assert client is None


def test_resolve_redis_explicit_raises():
    config = BenchmarkConfig(backend="redis")
    with pytest.raises(RuntimeError, match="Redis is unavailable"):
        resolve_redis(config, _FailingPing())


def test_resolve_redis_both_skips():
    config = BenchmarkConfig(backend="both")
    storage, client = resolve_redis(config, _FailingPing())
    assert storage is None
    assert client is None


def test_build_redis_client_from_env(monkeypatch):
    monkeypatch.setenv("REDIS_URL", "redis://example.com:6379/2")
    monkeypatch.setenv("REDIS_PASSWORD", "secret")

    client = build_redis_client()

    assert client.connection_pool.connection_kwargs["password"] == "secret"


# ------------------------------------------------------- runner behaviour


def test_verify_limiter_passes_for_real_limiter():
    limiter = create_rate_limiter(algorithm="fixed_window", limit=5, window=60)
    assert verify_limiter(lambda: limiter, 5) is True


def test_verify_limiter_rejects_under_enforcement():
    with pytest.raises(RuntimeError):
        verify_limiter(lambda: _BrokenLimiter(), 5)


def test_run_scenario_memory_burst_respects_limit():
    config = BenchmarkConfig(
        backend="memory",
        algorithm="fixed_window",
        traffic="burst",
        requests=100,
        limit=10,
        window=60,
    )

    result = run_scenario(config, "fixed_window", "burst", 1)

    assert result["backend"] == "memory"
    assert result["algorithm"] == "fixed_window"
    assert result["traffic"] == "burst"
    assert result["concurrency"] == 1
    assert result["requests"] == 100
    assert result["allowed"] == 10
    assert result["rejected"] == 90
    assert result["rps"] > 0


def test_run_benchmarks_memory_cartesian():
    config = BenchmarkConfig(
        backend="memory",
        algorithm="sliding_window",
        traffic="burst",
        requests=10,
        limit=5,
        concurrency="1,10",
        format="table",
    )

    results = run_benchmarks(config)

    assert len(results) == 2
    assert {item["concurrency"] for item in results} == {1, 10}
    assert all(item["allowed"] == 5 for item in results)


def test_run_scenario_redis_burst_respects_limit():
    pytest.skip(REDIS_REASON) if not REDIS_AVAILABLE else None

    config = BenchmarkConfig(
        backend="redis",
        algorithm="token_bucket",
        traffic="burst",
        requests=100,
        limit=10,
        window=60,
    )

    redis_client = build_redis_client()
    from app.storage.redis_storage import RedisStorage

    storage = RedisStorage(redis_client)

    result = run_scenario(
        config,
        "token_bucket",
        "burst",
        1,
        storage=storage,
        redis_client=redis_client,
    )

    assert result["backend"] == "redis"
    assert result["allowed"] == 10
    assert result["rejected"] == 90

    leftovers = list(
        redis_client.scan_iter(match="rateguard:token_bucket:bench:*", count=500)
    )
    assert leftovers == []


def test_run_scenario_redis_cleanup_all_algorithms():
    pytest.skip(REDIS_REASON) if not REDIS_AVAILABLE else None

    redis_client = build_redis_client()
    from app.storage.redis_storage import RedisStorage

    storage = RedisStorage(redis_client)
    config = BenchmarkConfig(
        backend="redis",
        traffic="burst",
        requests=20,
        limit=5,
        window=60,
    )

    for algorithm in ALGORITHMS:
        run_scenario(
            config,
            algorithm,
            "burst",
            1,
            storage=storage,
            redis_client=redis_client,
        )

    leftovers = list(
        redis_client.scan_iter(match="rateguard:*:bench:*", count=500)
    )
    assert leftovers == []