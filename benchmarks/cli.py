"""Command-line parsing and validation for the benchmark suite."""

import argparse

from benchmarks.matrix import render_table, run_matrix
from benchmarks.redis_backend import RedisUnavailableError
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
        if results:
            print(render_table(results))
        if failure is not None:
            scenario, exc = failure
            print(
                f"ERROR: {scenario['backend']}/{scenario['algorithm']}: "
                f"{exc}"
            )
            return 1
        return 0
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
    print(f"Backend: {result['backend']}")
    print(f"Algorithm: {result['algorithm']}")
    print(f"Requests: {result['requests']}")
    print(f"Allowed: {result['allowed']}")
    print(f"Rejected: {result['rejected']}")
    print(f"Throughput: {result['throughput_rps']:.1f} req/s")
    print(f"Average latency: {result['avg_ms']:.3f} ms")
    print(f"P50: {result['p50_ms']:.3f} ms")
    print(f"P95: {result['p95_ms']:.3f} ms")
    print(f"P99: {result['p99_ms']:.3f} ms")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
