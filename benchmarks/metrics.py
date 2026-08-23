"""Latency statistics helpers."""

import math


def percentile(samples_ms, pct) -> float:
    if not samples_ms:
        raise ValueError("percentile requires at least one sample")
    if not 0 <= pct <= 100:
        raise ValueError("pct must be between 0 and 100")
    ordered = sorted(samples_ms)
    rank = math.ceil(pct / 100 * len(ordered))
    rank = min(max(rank, 1), len(ordered))
    return ordered[rank - 1]


def latency_summary_ms(samples_ms) -> dict:
    if not samples_ms:
        raise ValueError("latency summary requires at least one sample")
    return {
        "avg_ms": sum(samples_ms) / len(samples_ms),
        "p50_ms": percentile(samples_ms, 50),
        "p95_ms": percentile(samples_ms, 95),
        "p99_ms": percentile(samples_ms, 99),
    }
