import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from benchmark.config import (
    ALGORITHMS,
    DEFAULT_CONCURRENCIES,
    TRAFFIC_PATTERNS,
    BenchmarkConfig,
)
from benchmark.results import save_results, to_table
from benchmark.runner import run_benchmarks
from benchmark.traffic_patterns import TimedLimiter, burst_traffic


def run_benchmark(name, limiter, requests):
    """Compatibility helper: single in-memory scenario, old result shape."""
    timed = TimedLimiter(limiter)
    start = time.perf_counter()
    burst_traffic(timed, requests)
    execution_time = time.perf_counter() - start

    return {
        "algorithm": name,
        "requests": requests,
        "allowed": timed.allowed,
        "blocked": timed.rejected,
        "execution_time": execution_time,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m benchmark.benchmark",
        description="RateGuard benchmark: memory and Redis rate limiters.",
    )

    parser.add_argument(
        "--backend",
        choices=["memory", "redis", "both"],
        default="memory",
        help="limiter backend to benchmark (default: memory)",
    )
    parser.add_argument(
        "--algorithm",
        choices=["all"] + ALGORITHMS,
        default="all",
        help="rate-limit algorithm (default: all)",
    )
    parser.add_argument(
        "--traffic",
        choices=["all"] + TRAFFIC_PATTERNS,
        default="burst",
        help="traffic pattern (default: burst)",
    )
    parser.add_argument(
        "--requests",
        type=int,
        default=1000,
        help="total requests per scenario (default: 1000)",
    )
    parser.add_argument(
        "--concurrency",
        default="1",
        help="comma-separated worker counts, or 'all' for 1,10,50 "
        "(default: 1)",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=100,
        help="rate limit per window (default: 100)",
    )
    parser.add_argument(
        "--window",
        type=int,
        default=60,
        help="rate limit window in seconds (default: 60)",
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=None,
        help="seconds between requests for paced traffic; auto per "
        "traffic pattern when unset",
    )
    parser.add_argument(
        "--redis-url",
        default=None,
        help="override REDIS_URL for the Redis benchmark",
    )
    parser.add_argument(
        "--output-dir",
        default="benchmark/results",
        help="directory for saved results (default: benchmark/results)",
    )
    parser.add_argument(
        "--format",
        choices=["table", "csv", "json", "all"],
        default="all",
        help="output: print table and/or save CSV/JSON (default: all)",
    )

    return parser


def config_from_args(args: argparse.Namespace) -> BenchmarkConfig:
    return BenchmarkConfig(
        backend=args.backend,
        algorithm=args.algorithm,
        traffic=args.traffic,
        requests=args.requests,
        concurrency=args.concurrency,
        limit=args.limit,
        window=args.window,
        interval=args.interval,
        redis_url=args.redis_url,
        output_dir=args.output_dir,
        format=args.format,
    )


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    config = config_from_args(args)

    print(
        f"RateGuard Benchmark - backend={config.backend} "
        f"algorithm={config.algorithm} traffic={config.traffic} "
        f"requests={config.requests} concurrency={config.concurrency} "
        f"limit={config.limit} window={config.window}s"
    )
    print("=" * 80)

    try:
        results = run_benchmarks(config)
    except RuntimeError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    if not results:
        print("No benchmark scenarios to run.")
        return 0

    print()
    print(to_table(results))
    print()

    if config.format in ("csv", "json", "all"):
        paths = save_results(results, config.output_dir, config.format)
        for kind, path in paths.items():
            print(f"Saved {kind}: {path}")

    return 0


if __name__ == "__main__":
    sys.exit(main())