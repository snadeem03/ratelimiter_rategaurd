"""Tests for --all matrix execution and the comparison table."""

import pytest

from benchmarks.cli import main, resolve_plan, build_parser
from benchmarks.matrix import format_result_row, render_table, run_matrix
from benchmarks.redis_backend import (
    RedisUnavailableError,
    cleanup_run_keys,
)

ALL_ALGORITHMS = [
    "fixed_window",
    "sliding_window",
    "token_bucket",
    "leaky_bucket",
]


def all_scenarios():
    return resolve_plan(build_parser().parse_args(["--all"]))["scenarios"]


class TestMatrixExpansion:
    def test_all_expands_to_exactly_eight_scenarios(self):
        scenarios = all_scenarios()
        assert len(scenarios) == 8

    def test_matrix_contains_memory_and_redis_for_every_algorithm(self):
        pairs = {(s["backend"], s["algorithm"]) for s in all_scenarios()}
        expected = {
            (backend, algorithm)
            for backend in ("memory", "redis")
            for algorithm in ALL_ALGORITHMS
        }
        assert pairs == expected

    def test_no_scenario_is_duplicated(self):
        scenarios = all_scenarios()
        keys = [(s["backend"], s["algorithm"]) for s in scenarios]
        assert len(keys) == len(set(keys))


class TestMatrixExecution:
    def test_all_eight_scenarios_execute_and_collect_in_order(self):
        scenarios = all_scenarios()
        results, failure = run_matrix(scenarios, 40, 2)
        try:
            assert failure is None
            assert len(results) == 8
            assert [
                (r["backend"], r["algorithm"]) for r in results
            ] == [(s["backend"], s["algorithm"]) for s in scenarios]
            for result in results:
                assert result["requests"] == 40
                assert result["concurrency"] == 2
                assert result["allowed"] + result["rejected"] == 40
                assert result["elapsed_s"] > 0
        finally:
            from app.core.redis_client import get_redis
            from benchmarks.redis_backend import run_key_pattern

            for result in results:
                if result["backend"] == "redis":
                    cleanup_run_keys(get_redis(), result["run_id"])

    def test_results_carry_their_own_run_ids(self):
        from app.core.redis_client import get_redis

        results, failure = run_matrix(all_scenarios(), 20, 1)
        try:
            assert failure is None
            redis_ids = {
                r["run_id"] for r in results if r["backend"] == "redis"
            }
            assert len(redis_ids) == 4
            memory = [r for r in results if r["backend"] == "memory"]
            assert all("run_id" not in r for r in memory)
        finally:
            for result in results:
                if result["backend"] == "redis":
                    cleanup_run_keys(get_redis(), result["run_id"])


class TestTable:
    def _sample_result(self, backend, algorithm):
        return {
            "backend": backend,
            "algorithm": algorithm,
            "requests": 100,
            "allowed": 40,
            "rejected": 60,
            "throughput_rps": 1234.5,
            "avg_ms": 0.8,
            "p50_ms": 0.7,
            "p95_ms": 1.2,
            "p99_ms": 1.9,
        }

    def test_row_contains_all_metrics(self):
        row = format_result_row(
            self._sample_result("memory", "token_bucket")
        )
        for fragment in (
            "token_bucket",
            "memory",
            "100",
            "40",
            "60",
            "1,234.5",
            "0.800",
            "1.200",
            "1.900",
        ):
            assert fragment in row

    def test_table_contains_header_and_every_scenario(self):
        rows = [
            self._sample_result(backend, algorithm)
            for backend in ("memory", "redis")
            for algorithm in ALL_ALGORITHMS
        ]
        table = render_table(rows)
        lines = table.splitlines()
        assert len(lines) == 2 + 8
        header = lines[0]
        for column in (
            "Algorithm", "Backend", "Requests", "Allowed", "Rejected",
            "Throughput(req/s)", "Avg(ms)", "P50(ms)", "P95(ms)", "P99(ms)",
        ):
            assert column in header
        body = "\n".join(lines[2:])
        for backend in ("memory", "redis"):
            for algorithm in ALL_ALGORITHMS:
                assert f"{algorithm:<14} {backend}" in body

    def test_table_is_terminal_friendly_width(self):
        rows = [self._sample_result("memory", "fixed_window")]
        width = max(len(line) for line in render_table(rows).splitlines())
        assert width <= 120


class TestRedisFailureHandling:
    def test_failure_stops_matrix_and_reports_offending_scenario(
        self, monkeypatch
    ):
        def fake_run_scenario(backend, algorithm, requests, concurrency):
            if backend == "redis":
                raise RedisUnavailableError("Redis is not reachable; ...")
            return {
                "backend": backend,
                "algorithm": algorithm,
                "requests": requests,
                "concurrency": concurrency,
                "elapsed_s": 0.01,
                "allowed": requests,
                "rejected": 0,
                "throughput_rps": 99.9,
                "avg_ms": 0.1,
                "p50_ms": 0.1,
                "p95_ms": 0.1,
                "p99_ms": 0.1,
            }

        monkeypatch.setattr(
            "benchmarks.matrix.run_scenario", fake_run_scenario
        )
        results, failure = run_matrix(all_scenarios(), 10, 1)
        assert len(results) == 4
        assert all(r["backend"] == "memory" for r in results)
        assert failure is not None
        scenario, exc = failure
        assert scenario["backend"] == "redis"
        assert scenario["algorithm"] == "fixed_window"
        assert isinstance(exc, RedisUnavailableError)

    def test_cli_all_exits_nonzero_without_fake_redis_rows(
        self, capsys, monkeypatch
    ):
        def fake_run_scenario(backend, algorithm, requests, concurrency):
            if backend == "redis":
                raise RedisUnavailableError("down")
            return {
                "backend": backend,
                "algorithm": algorithm,
                "requests": requests,
                "concurrency": concurrency,
                "elapsed_s": 0.01,
                "allowed": 5,
                "rejected": requests - 5,
                "throughput_rps": 1.0,
                "avg_ms": 0.1,
                "p50_ms": 0.1,
                "p95_ms": 0.1,
                "p99_ms": 0.1,
            }

        monkeypatch.setattr(
            "benchmarks.matrix.run_scenario", fake_run_scenario
        )
        exit_code = main(["--all"])
        out = capsys.readouterr().out
        assert exit_code == 1
        assert "ERROR: redis/fixed_window" in out
        table_rows = [
            line for line in out.splitlines() if "memory" in line
        ]
        redis_rows = [
            line for line in out.splitlines() if " redis " in line
        ]
        assert table_rows == []
        assert redis_rows == []


class TestSingleScenarioUnchanged:
    def test_single_scenarios_do_not_render_a_table(self, capsys):
        exit_code = main(
            ["--backend", "memory", "--algorithm", "token_bucket",
             "--requests", "50", "--concurrency", "1"]
        )
        out = capsys.readouterr().out
        assert exit_code == 0
        assert "Backend: memory" in out
        assert "Algorithm:" not in out.split("Algorithm: token_bucket")[0]
        assert "-" * 20 not in out
        assert "P99: " in out

    @pytest.mark.parametrize("backend", ["memory", "redis"])
    def test_explicit_backend_runs_only_that_backend(self, capsys, backend):
        exit_code = main(
            ["--backend", backend, "--algorithm", "fixed_window",
             "--requests", "30", "--concurrency", "1"]
        )
        out = capsys.readouterr().out
        assert exit_code == 0
        assert f"Backend: {backend}" in out
        if backend == "redis":
            from app.core.redis_client import get_redis

            leftovers = list(get_redis().scan_iter("rateguard:*bench*"))
            assert leftovers == []
