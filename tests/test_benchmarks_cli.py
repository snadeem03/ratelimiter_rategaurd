"""Tests for the benchmarks CLI (parsing + validation only)."""

import pytest

from benchmarks.cli import (
    ALGORITHMS,
    BACKENDS,
    build_parser,
    main,
    resolve_plan,
    validate_args,
)


def parse(argv=None):
    args = build_parser().parse_args(argv)
    errors = validate_args(args)
    return args, errors


class TestDefaults:
    def test_no_arguments_uses_defaults(self):
        args, errors = parse([])
        assert errors == []
        assert args.backend is None
        assert args.algorithm is None
        assert args.requests == 1000
        assert args.concurrency == 1
        assert args.all is False

    def test_default_plan_is_single_memory_sliding_window_scenario(self):
        args, _ = parse([])
        plan = resolve_plan(args)
        assert plan["scenarios"] == [
            {"backend": "memory", "algorithm": "sliding_window"}
        ]
        assert plan["requests"] == 1000
        assert plan["concurrency"] == 1


class TestParsing:
    def test_explicit_backend_and_algorithm(self):
        args, errors = parse(["--backend", "redis", "--algorithm", "token_bucket"])
        assert errors == []
        plan = resolve_plan(args)
        assert plan["scenarios"] == [
            {"backend": "redis", "algorithm": "token_bucket"}
        ]

    def test_every_supported_combination_is_valid(self):
        for backend in BACKENDS:
            for algorithm in ALGORITHMS:
                _, errors = parse(
                    ["--backend", backend, "--algorithm", algorithm]
                )
                assert errors == []

    def test_unknown_backend_rejected_by_argparse(self, capsys):
        with pytest.raises(SystemExit) as excinfo:
            parse(["--backend", "memcached"])
        assert excinfo.value.code == 2
        assert "invalid choice" in capsys.readouterr().err

    def test_unknown_algorithm_rejected_by_argparse(self, capsys):
        with pytest.raises(SystemExit) as excinfo:
            parse(["--algorithm", "hyperloglog"])
        assert excinfo.value.code == 2
        assert "invalid choice" in capsys.readouterr().err

    def test_non_integer_requests_rejected_by_argparse(self, capsys):
        with pytest.raises(SystemExit) as excinfo:
            parse(["--requests", "lots"])
        assert excinfo.value.code == 2
        assert "invalid int value" in capsys.readouterr().err

    def test_non_integer_concurrency_rejected_by_argparse(self, capsys):
        with pytest.raises(SystemExit) as excinfo:
            parse(["--concurrency", "x"])
        assert excinfo.value.code == 2
        assert "invalid int value" in capsys.readouterr().err


class TestValidation:
    @pytest.mark.parametrize("value", ["0", "-1"])
    def test_requests_below_one_rejected(self, value):
        _, errors = parse(["--requests", value])
        assert "--requests must be >= 1" in errors

    @pytest.mark.parametrize("value", ["0", "-4"])
    def test_concurrency_below_one_rejected(self, value):
        _, errors = parse(["--concurrency", value])
        assert "--concurrency must be >= 1" in errors

    def test_all_with_explicit_backend_rejected(self):
        _, errors = parse(["--all", "--backend", "redis"])
        assert any("--all cannot be combined" in e for e in errors)

    def test_all_with_explicit_algorithm_rejected(self):
        _, errors = parse(["--all", "--algorithm", "fixed_window"])
        assert any("--all cannot be combined" in e for e in errors)


class TestAllFlag:
    def test_all_expands_full_matrix(self):
        args, errors = parse(["--all"])
        assert errors == []
        plan = resolve_plan(args)
        assert len(plan["scenarios"]) == len(BACKENDS) * len(ALGORITHMS)
        expected = {
            (b, a) for b in BACKENDS for a in ALGORITHMS
        }
        actual = {
            (s["backend"], s["algorithm"]) for s in plan["scenarios"]
        }
        assert actual == expected


class TestMain:
    def test_default_invocation_executes_single_scenario(self, capsys):
        assert main([]) == 0
        out = capsys.readouterr().out
        assert "Backend: memory" in out
        assert "Algorithm: sliding_window" in out
        assert "Requests: 1000" in out

    def test_invalid_args_exit_with_code_two(self, capsys):
        with pytest.raises(SystemExit) as excinfo:
            main(["--requests", "0"])
        assert excinfo.value.code == 2
        assert "--requests must be >= 1" in capsys.readouterr().err

    def test_help_lists_all_flags(self, capsys):
        with pytest.raises(SystemExit) as excinfo:
            main(["--help"])
        assert excinfo.value.code == 0
        out = capsys.readouterr().out
        for flag in (
            "--backend",
            "--algorithm",
            "--requests",
            "--concurrency",
            "--all",
        ):
            assert flag in out
