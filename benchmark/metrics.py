"""Result metrics: percentiles, throughput, and latency summaries."""


def percentile(values: list[float], p: float) -> float:
    """Linear-interpolation percentile (0 <= p <= 100).

    Mirrors numpy's default ``percentile`` behaviour so results are stable
    regardless of sample size.
    """
    if not values:
        return 0.0

    ordered = sorted(values)

    if len(ordered) == 1:
        return float(ordered[0])

    k = (len(ordered) - 1) * (p / 100.0)
    lower = int(k)
    upper = lower + 1

    if upper >= len(ordered):
        return float(ordered[-1])

    fraction = k - lower

    return ordered[lower] + fraction * (ordered[upper] - ordered[lower])


def summarize(
    allowed: int,
    rejected: int,
    latencies_ms: list[float],
    elapsed: float,
) -> dict:
    """Collapse raw counts and latencies into a comparable result row."""
    total = allowed + rejected

    if latencies_ms:
        average = sum(latencies_ms) / len(latencies_ms)
    else:
        average = 0.0

    return {
        "requests": total,
        "allowed": allowed,
        "rejected": rejected,
        "elapsed": round(elapsed, 6),
        "rps": round(total / elapsed, 2) if elapsed > 0 else 0.0,
        "avg_latency_ms": round(average, 4),
        "p50_latency_ms": round(percentile(latencies_ms, 50), 4),
        "p95_latency_ms": round(percentile(latencies_ms, 95), 4),
        "p99_latency_ms": round(percentile(latencies_ms, 99), 4),
    }