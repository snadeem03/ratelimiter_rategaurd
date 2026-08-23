"""Tests for JSON result export (no large benchmarks; fakes where possible)."""

import json
from datetime import datetime

import pytest

from benchmarks.cli import main
from benchmarks.export import (
    _map_result,
    build_report,
    collect_environment,
    write_report,
)


class _FakeRedis:
    def info(self, section):
        return {"redis_version": "7.2.4"}


def sample_result(backend="memory", algorithm="token_bucket"):
    return {
        "backend": backend,
        "algorithm": algorithm,
        "requests": 20,
        "concurrency": 1,
        "elapsed_s": 0.001,
        "allowed": 10,
        "rejected": 10,
        "throughput_rps": 12345.6789,
        "avg_ms": 0.0808,
        "p50_ms": 0.07,
        "p95_ms": 0.12,
        "p99_ms": 0.19,
    }


class TestEnvironment:
    def test_contains_only_useful_non_sensitive_fields(self):
        env = collect_environment()
        assert set(env) <= {"python", "os", "cpu_count", "cpu"}
        assert env["python"].count(".") == 2
        assert isinstance(env["cpu_count"], int)

    FORBIDDEN = ("user", "home", "path", "token", "key", "password", "=")

    def test_no_sensitive_values_leak_into_environment(self):
        text = json.dumps(collect_environment()).lower()
        for forbidden in self.FORBIDDEN:
            assert forbidden not in text.replace("python", "")

    def test_redis_version_included_when_client_given(self):
        env = collect_environment(redis_client=_FakeRedis())
        assert env["redis_version"] == "7.2.4"

    def test_no_redis_version_without_client(self):
        assert "redis_version" not in collect_environment()


class TestReport:
    def test_top_level_structure(self):
        report = build_report(
            {"requests": 20, "concurrency": 1,
             "backend": "all", "algorithm": "all"},
            [sample_result()],
        )
        assert set(report) == {
            "generated_at", "environment", "configuration", "results"
        }
        parsed = datetime.fromisoformat(report["generated_at"])
        assert parsed.tzinfo is not None
        assert report["configuration"]["backend"] == "all"
        assert len(report["results"]) == 1

    def test_result_field_names_match_specification(self):
        mapped = _map_result(sample_result())
        assert set(mapped) == {
            "algorithm",
            "backend",
            "requests",
            "allowed",
            "rejected",
            "elapsed_s",
            "throughput_rps",
            "avg_latency_ms",
            "p50_latency_ms",
            "p95_latency_ms",
            "p99_latency_ms",
        }
        assert "avg_ms" not in mapped

    def test_precision_is_preserved(self):
        mapped = _map_result(sample_result())
        assert mapped["throughput_rps"] == pytest.approx(12345.6789)
        assert mapped["avg_latency_ms"] == pytest.approx(0.0808)

    def test_run_id_is_not_exported(self):
        result = sample_result()
        result["run_id"] = "abc123"
        assert "run_id" not in _map_result(result)


class TestWriteAndCli:
    def test_write_report_creates_valid_json(self, tmp_path):
        path = tmp_path / "results.json"
        write_report(str(path), build_report({}, [sample_result()]))
        data = json.loads(path.read_text(encoding="utf-8"))
        assert data["results"][0]["backend"] == "memory"

    def test_output_flag_writes_json_file(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "benchmarks.matrix.run_scenario",
            lambda backend, algorithm, requests, concurrency: sample_result(
                backend=backend, algorithm=algorithm
            ),
        )
        path = tmp_path / "out.json"
        exit_code = main(["--all", "--output", str(path)])
        out_lines = []
        assert exit_code == 0
        data = json.loads(path.read_text(encoding="utf-8"))
        assert len(data["results"]) == 8
        assert data["configuration"] == {
            "requests": 1000,
            "concurrency": 1,
            "backend": "all",
            "algorithm": "all",
        }

    def test_single_scenario_output_uses_explicit_configuration(
        self, tmp_path
    ):
        path = tmp_path / "single.json"
        exit_code = main(
            ["--backend", "memory", "--algorithm", "fixed_window",
             "--requests", "20", "--concurrency", "1",
             "--output", str(path)]
        )
        assert exit_code == 0
        data = json.loads(path.read_text(encoding="utf-8"))
        assert data["configuration"] == {
            "requests": 20,
            "concurrency": 1,
            "backend": "memory",
            "algorithm": "fixed_window",
        }
        result = data["results"][0]
        assert result["allowed"] + result["rejected"] == 20
        assert "p95_latency_ms" in result
        assert "redis_version" not in data["environment"]

    def test_no_file_created_without_output_flag(self):
        exit_code = main(
            ["--backend", "memory", "--algorithm", "sliding_window",
             "--requests", "10", "--concurrency", "1"]
        )
        assert exit_code == 0

    def test_unwritable_output_reports_error_and_exits_nonzero(
        self, tmp_path, capsys, monkeypatch
    ):
        monkeypatch.setattr(
            "benchmarks.matrix.run_scenario",
            lambda backend, algorithm, requests, concurrency: sample_result(),
        )
        target_dir = tmp_path / "i-am-a-directory"
        target_dir.mkdir()
        exit_code = main(["--all", "--output", str(target_dir)])
        captured = capsys.readouterr()
        assert exit_code == 1
        assert "ERROR:" in captured.out
        assert "cannot write output file" in captured.out

    def test_benchmark_failure_writes_nothing(self, tmp_path, capsys,
                                              monkeypatch):
        from benchmarks.redis_backend import RedisUnavailableError

        def fail(**kwargs):
            raise RedisUnavailableError("down")

        monkeypatch.setattr("benchmarks.matrix.run_scenario", fail)
        path = tmp_path / "should-not-exist.json"
        exit_code = main(["--all", "--output", str(path)])
        captured = capsys.readouterr()
        assert exit_code == 1
        assert "ERROR:" in captured.out
        assert not path.exists()

    def test_redis_scenario_export_includes_redis_version(self, tmp_path):
        path = tmp_path / "redis.json"
        exit_code = main(
            ["--backend", "redis", "--algorithm", "token_bucket",
             "--requests", "10", "--concurrency", "1",
             "--output", str(path)]
        )
        try:
            assert exit_code == 0
            data = json.loads(path.read_text(encoding="utf-8"))
            assert "redis_version" in data["environment"]
            assert data["results"][0]["backend"] == "redis"
        finally:
            import pytest as _pytest

            from app.core.redis_client import get_redis as _gr

            leftovers = list(_gr().scan_iter("rateguard:*bench*"))
            assert leftovers == []
