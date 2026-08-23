"""Full-matrix execution (--all): sequential, reproducible, no fakes."""

from benchmarks.runner import run_scenario

TABLE_COLUMNS = [
    ("Algorithm", 14),
    ("Backend", 8),
    ("Requests", 9),
    ("Allowed", 8),
    ("Rejected", 9),
    ("Throughput(req/s)", 18),
    ("Avg(ms)", 9),
    ("P50(ms)", 9),
    ("P95(ms)", 9),
    ("P99(ms)", 9),
]


def run_matrix(scenarios, requests: int, concurrency: int):
    results = []
    for scenario in scenarios:
        try:
            result = run_scenario(
                backend=scenario["backend"],
                algorithm=scenario["algorithm"],
                requests=requests,
                concurrency=concurrency,
            )
        except Exception as exc:
            return results, (scenario, exc)
        results.append(result)
    return results, None


def format_result_row(result: dict) -> str:
    return (
        f"{result['algorithm']:<14} {result['backend']:<8} "
        f"{result['requests']:>9} {result['allowed']:>8} "
        f"{result['rejected']:>9} {result['throughput_rps']:>18,.1f} "
        f"{result['avg_ms']:>9.3f} {result['p50_ms']:>9.3f} "
        f"{result['p95_ms']:>9.3f} {result['p99_ms']:>9.3f}"
    )


def render_table(results) -> str:
    header = " ".join(
        f"{name:>{width}}" if index >= 2 else f"{name:<{width}}"
        for index, (name, width) in enumerate(TABLE_COLUMNS)
    )
    lines = [header.rstrip(), "-" * len(header)]
    lines.extend(format_result_row(r) for r in results)
    return "\n".join(lines)
