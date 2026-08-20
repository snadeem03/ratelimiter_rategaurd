"""Benchmark configuration and scenario selection."""

from dataclasses import dataclass

ALGORITHMS = [
    "fixed_window",
    "sliding_window",
    "token_bucket",
    "leaky_bucket",
]

TRAFFIC_PATTERNS = ["normal", "burst", "sustained"]

BACKENDS = ["memory", "redis"]

DEFAULT_CONCURRENCIES = [1, 10, 50]

ALGORITHM_CAVEATS = {
    "fixed_window": "same limit every window; allows a burst at the window boundary",
    "sliding_window": "continuous window; slightly more Lua work per request",
    "token_bucket": "refill_rate = limit/window; burst capacity = limit",
    "leaky_bucket": "leak_rate = limit/window; drains oldest requests first",
}


@dataclass
class BenchmarkConfig:
    """Describe the set of benchmark scenarios to run.

    ``algorithm``/``traffic``/``backend`` accept ``"all"`` to expand into
    every option. ``concurrency`` is a comma-separated list (or ``"all"``)
    expanded into one scenario per value.
    """

    backend: str = "memory"
    algorithm: str = "all"
    traffic: str = "burst"
    requests: int = 1000
    concurrency: str = "1"
    limit: int = 100
    window: int = 60
    interval: float | None = None
    redis_url: str | None = None
    output_dir: str = "benchmark/results"
    format: str = "all"

    def algorithms(self) -> list[str]:
        if self.algorithm == "all":
            return list(ALGORITHMS)
        return [self.algorithm]

    def traffic_patterns(self) -> list[str]:
        if self.traffic == "all":
            return list(TRAFFIC_PATTERNS)
        return [self.traffic]

    def concurrencies(self) -> list[int]:
        if self.concurrency == "all":
            return list(DEFAULT_CONCURRENCIES)
        values = [
            int(item.strip())
            for item in self.concurrency.split(",")
            if item.strip()
        ]
        if not values:
            raise ValueError("concurrency must list at least one value")
        return values

    def backends(self) -> list[str]:
        if self.backend == "both":
            return list(BACKENDS)
        return [self.backend]