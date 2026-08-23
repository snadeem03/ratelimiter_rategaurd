"""Command-line parsing and validation for the benchmark suite."""

import argparse

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
    print(f"Scenarios: {len(plan['scenarios'])}")
    for scenario in plan["scenarios"]:
        print(
            f"  {scenario['backend']:>6} / {scenario['algorithm']:<14}"
            f" requests={plan['requests']} concurrency={plan['concurrency']}"
        )
    print("Execution is not implemented yet.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
