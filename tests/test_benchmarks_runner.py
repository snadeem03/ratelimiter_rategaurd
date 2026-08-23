"""Tests for benchmark metrics, single-scenario execution, and CLI gating."""

import pytest

from benchmarks.cli import main
from benchmarks.metrics import latency_summary_ms, percentile
from benchmarks.runner import build_limiter, run_scenario

ALL_ALGORITHMS = [
    "fixed_window",
    "sliding_window",
    "token_bucket",
    "leaky_bucket",
]

RESULT_KEYS = {
    "backend",
    "algorithm",
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


class TestPercentile:
    def test_percentiles_of_one_to_hundred(self):
        samples = list(range(1, 101))
        assert percentile(samples, 50) == 50
        assert percentile(samples, 95) == 95
        assert percentile(samples, 99) == 99
        assert percentile(samples, 0) == 1
        assert percentile(samples, 100) == 100

    def test_unsorted_input_is_sorted_internally(self):
        assert percentile([30, 10, 20], 50) == 20

    def test_single_sample(self):
        assert percentile([7.5], 50) == 7.5
        assert percentile([7.5], 99) == 7.5

    def test_empty_samples_raise(self):
        with pytest.raises(ValueError):
            percentile([], 50)

    def test_pct_out_of_range_raises(self):
        with pytest.raises(ValueError):
            percentile([1.0], 101)


class TestLatencySummary:
    def test_summary_structure_and_ordering(self):
        samples = [float(x) for x in range(1, 101)]
        summary = latency_summary_ms(samples)
        assert set(summary) == {"avg_ms", "p50_ms", "p95_ms", "p99_ms"}
        assert summary["avg_ms"] == pytest.approx(50.5)
        assert (
            summary["p50_ms"]
            <= summary["p95_ms"]
            <= summary["p99_ms"]
        )

    def test_empty_samples_raise(self):
        with pytest.raises(ValueError):
            latency_summary_ms([])


class TestBuildLimiter:
    @pytest.mark.parametrize("algorithm", ALL_ALGORITHMS)
    def test_builds_real_rateguard_limiters_for_every_algorithm(
        self, algorithm
    ):
        limiter = build_limiter(algorithm)
        assert type(limiter).__module__ == f"app.algorithms.{algorithm}"
        assert callable(limiter.allow_request)
        assert limiter.allow_request() in (True, False)
        assert isinstance(limiter.remaining_requests(), int)

    def test_unknown_algorithm_rejected(self):
        with pytest.raises(ValueError):
            build_limiter("sliding_log")


class TestRunScenario:
    def test_result_structure(self):
        result = run_scenario("memory", "token_bucket", 100, 1)
        assert RESULT_KEYS <= set(result)
        assert result["backend"] == "memory"
        assert result["algorithm"] == "token_bucket"
        assert result["requests"] == 100
        assert result["concurrency"] == 1

    def test_counts_sum_to_requests(self):
        result = run_scenario("memory", "fixed_window", 200, 2)
        assert result["allowed"] + result["rejected"] == 200
        assert result["allowed"] > 0

    def test_fixed_window_respects_limit(self):
        result = run_scenario("memory", "fixed_window", 150, 1)
        assert result["allowed"] == 100
        assert result["rejected"] == 50

    @pytest.mark.parametrize("algorithm", ALL_ALGORITHMS)
    def test_every_memory_algorithm_executes(self, algorithm):
        result = run_scenario("memory", algorithm, 120, 2)
        assert result["allowed"] + result["rejected"] == 120
        assert result["elapsed_s"] > 0

    def test_latency_statistics_are_sane(self):
        result = run_scenario("memory", "token_bucket", 200, 1)
        assert result["throughput_rps"] > 0
        for key in ("avg_ms", "p50_ms", "p95_ms", "p99_ms"):
            assert result[key] >= 0
        assert (
            result["p50_ms"]
            <= result["p95_ms"]
            <= result["p99_ms"]
        )

    def test_concurrency_is_controlled(self):
        result = run_scenario("memory", "leaky_bucket", 80, 4)
        assert result["requests"] == 80
        assert result["concurrency"] == 4


class TestUnsupportedBackend:
    def test_runner_rejects_unknown_backends(self):
        with pytest.raises(NotImplementedError, match="not implemented"):
            run_scenario("memcached", "token_bucket", 10, 1)


class TestCliExecution:
    def test_memory_scenario_prints_full_report(self, capsys):
        exit_code = main(
            ["--backend", "memory", "--algorithm", "token_bucket",
             "--requests", "100", "--concurrency", "1"]
        )
        out = capsys.readouterr().out
        assert exit_code == 0
        assert "Backend: memory" in out
        assert "Algorithm: token_bucket" in out
        assert "Requests: 100" in out
        assert "Allowed: " in out
        assert "Rejected: " in out
        assert "Throughput: " in out
        assert "req/s" in out
        assert "Average latency: " in out
        assert "P50: " in out
        assert "P95: " in out
        assert "P99: " in out

    def test_redis_unavailable_fails_clearly_with_nonzero_exit(
        self, capsys, monkeypatch
    ):
        from benchmarks.redis_backend import RedisUnavailableError

        def fail(*args, **kwargs):
            raise RedisUnavailableError("Redis is not reachable; ...")

        monkeypatch.setattr("benchmarks.cli.run_scenario", fail)
        exit_code = main(["--backend", "redis"])
        out = capsys.readouterr().out
        assert exit_code == 1
        assert "ERROR:" in out
        assert "not reachable" in out

    def test_all_execution_still_deferred(self, capsys):
        exit_code = main(["--all"])
        out = capsys.readouterr().out
        assert exit_code == 0
        assert "--all execution is not implemented yet." in out
