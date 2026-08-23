"""Tests for Redis-backed benchmarking (skipped when Redis is down,
except the availability/unavailable-behavior tests which never need a
server)."""

import pytest

from app.core.redis_client import get_redis
from benchmarks.redis_backend import (
    BENCH_MARKER,
    RedisUnavailableError,
    benchmark_client_id,
    build_run_limiter,
    cleanup_run_keys,
    new_run_id,
    redis_reachable,
    require_redis,
    run_key_pattern,
    run_redis_scenario,
)

try:
    get_redis().ping()
    REDIS_AVAILABLE = True
except Exception:
    REDIS_AVAILABLE = False

pytestmark = pytest.mark.skipif(
    not REDIS_AVAILABLE, reason="Redis is not available"
)

ALL_ALGORITHMS = [
    "fixed_window",
    "sliding_window",
    "token_bucket",
    "leaky_bucket",
]

RESULT_KEYS = {
    "backend",
    "algorithm",
    "run_id",
    "requests",
    "concurrency",
    "elapsed_s",
    "allowed",
    "rejected",
    "throughput_rps",
    "avg_ms",
    "p50_ms",
    "p95_ms",
    "p99_ms",
}


class _FailingClient:
    def ping(self):
        raise ConnectionError("connection refused")


class _OkClient:
    def ping(self):
        return True


class TestAvailability:
    def test_reachable_client_reports_true(self):
        assert redis_reachable(_OkClient()) is True

    def test_failing_client_reports_false(self):
        assert redis_reachable(_FailingClient()) is False

    def test_require_redis_raises_when_unreachable(self):
        with pytest.raises(RedisUnavailableError, match="not reachable"):
            require_redis(client=_FailingClient())

    def test_require_redis_returns_client_when_reachable(self):
        client = _OkClient()
        assert require_redis(client=client) is client


class TestRunNamespace:
    def test_new_run_ids_are_unique(self):
        assert new_run_id() != new_run_id()

    def test_client_id_embeds_marker_and_run_id(self):
        client_id = benchmark_client_id("abc123")
        assert client_id == f"{BENCH_MARKER}:abc123"

    @pytest.mark.parametrize("algorithm", ALL_ALGORITHMS)
    def test_keys_live_inside_the_run_namespace(self, algorithm):
        run_id = new_run_id()
        limiter = build_run_limiter(algorithm, get_redis(), run_id)
        limiter.allow_request()
        keys = list(get_redis().scan_iter(run_key_pattern(run_id)))
        try:
            assert len(keys) >= 1
            for key in keys:
                assert BENCH_MARKER in key and run_id in key
                assert key.startswith(f"rateguard:{algorithm}:")
        finally:
            cleanup_run_keys(get_redis(), run_id)


class TestConstruction:
    @pytest.mark.parametrize("algorithm", ALL_ALGORITHMS)
    def test_factory_builds_real_redis_limiters(self, algorithm):
        from app.algorithms.redis_fixed_window import (
            RedisFixedWindowRateLimiter,
        )
        from app.algorithms.redis_leaky_bucket import (
            RedisLeakyBucketRateLimiter,
        )
        from app.algorithms.redis_sliding_window import (
            RedisSlidingWindowRateLimiter,
        )
        from app.algorithms.redis_token_bucket import (
            RedisTokenBucketRateLimiter,
        )

        expected = {
            "fixed_window": RedisFixedWindowRateLimiter,
            "sliding_window": RedisSlidingWindowRateLimiter,
            "token_bucket": RedisTokenBucketRateLimiter,
            "leaky_bucket": RedisLeakyBucketRateLimiter,
        }
        run_id = new_run_id()
        limiter = build_run_limiter(algorithm, get_redis(), run_id)
        assert type(limiter) is expected[algorithm]


class TestExecution:
    @pytest.mark.parametrize("algorithm", ALL_ALGORITHMS)
    def test_all_four_algorithms_execute(self, algorithm):
        result = run_redis_scenario(algorithm, 60, 2)
        try:
            assert RESULT_KEYS <= set(result)
            assert result["backend"] == "redis"
            assert result["allowed"] + result["rejected"] == 60
            assert result["elapsed_s"] > 0
            assert result["throughput_rps"] > 0
        finally:
            cleanup_run_keys(get_redis(), result["run_id"])

    def test_fixed_window_enforces_limit(self):
        result = run_redis_scenario("fixed_window", 150, 1)
        try:
            assert result["allowed"] == 100
            assert result["rejected"] == 50
        finally:
            cleanup_run_keys(get_redis(), result["run_id"])

    @pytest.mark.parametrize("algorithm", ALL_ALGORITHMS)
    def test_no_leftover_keys_after_completion(self, algorithm):
        result = run_redis_scenario(algorithm, 30, 1)
        leftover = list(
            get_redis().scan_iter(run_key_pattern(result["run_id"]))
        )
        assert leftover == []


class TestIsolation:
    def test_separate_runs_have_isolated_state(self):
        first = run_redis_scenario("fixed_window", 120, 1)
        second = run_redis_scenario("fixed_window", 120, 1)
        try:
            assert first["run_id"] != second["run_id"]
            assert first["allowed"] == 100
            assert second["allowed"] == 100
        finally:
            cleanup_run_keys(get_redis(), first["run_id"])
            cleanup_run_keys(get_redis(), second["run_id"])

    def test_explicit_run_id_is_respected(self):
        run_id = new_run_id()
        result = run_redis_scenario(
            "token_bucket", 10, 1, run_id=run_id
        )
        try:
            assert result["run_id"] == run_id
        finally:
            cleanup_run_keys(get_redis(), run_id)
