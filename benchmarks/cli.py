"""Command-line parsing and validation for the benchmark suite."""

import argparse

from benchmarks.export import build_report, write_report
from benchmarks.matrix import render_table, run_matrix
from benchmarks.redis_backend import RedisUnavailableError, get_redis
from benchmarks.runner import run_scenario

BACKENDS = ["memory", "redis"]
ALGORITHMS = ["fixed_window", "sliding_window", "token_bucket", "leaky_bucket"]

DEFAULT_BACKEND = "memory"
DEFAULT_ALGORITHM = "sliding_window"
DEFAULT_REQUESTS = 1000
DEFAULT_CONCURRENCY = 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m benchmarks.run",
        description="RateGuard v1.2 benchmark suite.",
    )
    parser.add_argument(
        "--backend",
        choices=BACKENDS,
        default=None,
        help="limiter backend to benchmark (default: memory)",
    )
    parser.add_argument(
        "--algorithm",
        choices=ALGORITHMS,
        default=None,
        help="rate-limit algorithm (default: sliding_window)",
    )
    parser.add_argument(
        "--requests",
        type=int,
        default=DEFAULT_REQUESTS,
        help="total requests per scenario (default: 1000)",
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        default=DEFAULT_CONCURRENCY,
        help="number of concurrent workers (default: 1)",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="benchmark every backend x algorithm combination",
    )
    parser.add_argument(
        "--output",
        default=None,
        help="write results as JSON to this path (no file by default)",
    )
    return parser


def validate_args(args: argparse.Namespace) -> list:
    errors = []
    if args.requests < 1:
        errors.append("--requests must be >= 1")
    if args.concurrency < 1:
        errors.append("--concurrency must be >= 1")
    if args.all and (args.backend is not None or args.algorithm is not None):
        errors.append(
            "--all cannot be combined with explicit --backend or --algorithm"
        )
    return errors


def resolve_plan(args: argparse.Namespace) -> dict:
    if args.all:
        backends = BACKENDS
        algorithms = ALGORITHMS
    else:
        backends = [args.backend or DEFAULT_BACKEND]
        algorithms = [args.algorithm or DEFAULT_ALGORITHM]
    scenarios = [
        {"backend": backend, "algorithm": algorithm}
        for backend in backends
        for algorithm in algorithms
    ]
    return {
        "scenarios": scenarios,
        "requests": args.requests,
        "concurrency": args.concurrency,
    }


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    errors = validate_args(args)
    if errors:
        for message in errors:
            parser.error(message)
    plan = resolve_plan(args)
    if len(plan["scenarios"]) > 1:
        results, failure = run_matrix(
            plan["scenarios"], plan["requests"], plan["concurrency"]
        )
        if failure is not None:
            scenario, exc = failure
            print(
                f"ERROR: {scenario['backend']}/{scenario['algorithm']}: "
                f"{exc}"
            )
            return 1
        print(render_table(results))
        code = _write_output(
            args, results,
            backend="all", algorithm="all",
            ran_redis=True,
        )
        return code if code is not None else 0
    scenario = plan["scenarios"][0]
    try:
        result = run_scenario(
            backend=scenario["backend"],
            algorithm=scenario["algorithm"],
            requests=plan["requests"],
            concurrency=plan["concurrency"],
        )
    except RedisUnavailableError as exc:
        print(f"ERROR: {exc}")
        return 1
    result_list = [result]
    print(_format_single(result))
    exit_code = _write_output(
        args, result_list,
        backend=result["backend"], algorithm=result["algorithm"],
        ran_redis=result["backend"] == "redis",
    )
    if exit_code is not None:
        return exit_code
    return 0


def _format_single(result: dict) -> str:
    lines = [
        f"Backend: {result['backend']}",
        f"Algorithm: {result['algorithm']}",
        f"Requests: {result['requests']}",
        f"Allowed: {result['allowed']}",
        f"Rejected: {result['rejected']}",
        f"Throughput: {result['throughput_rps']:.1f} req/s",
        f"Average latency: {result['avg_ms']:.3f} ms",
        f"P50: {result['p50_ms']:.3f} ms",
        f"P95: {result['p95_ms']:.3f} ms",
        f"P99: {result['p99_ms']:.3f} ms",
    ]
    return "\n".join(lines)


def _write_output(args, results, backend: str, algorithm: str,
                  ran_redis: bool):
    if not args.output:
        return None
    configuration = {
        "requests": args.requests,
        "concurrency": args.concurrency,
        "backend": backend,
        "algorithm": algorithm,
    }
    redis_client = get_redis() if ran_redis else None
    report = build_report(configuration, results, redis_client=redis_client)
    try:
        write_report(args.output, report)
    except OSError as exc:
        print(f"ERROR: cannot write output file '{args.output}': {exc}")
        return 1
    print(f"Results written to {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
