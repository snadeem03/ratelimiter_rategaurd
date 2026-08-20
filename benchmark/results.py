"""Result rendering and persistence (table / CSV / JSON)."""

import csv
import io
import json
import os
from datetime import datetime, timezone

FIELDS = [
    "algorithm",
    "backend",
    "traffic",
    "concurrency",
    "requests",
    "allowed",
    "rejected",
    "elapsed",
    "rps",
    "avg_latency_ms",
    "p50_latency_ms",
    "p95_latency_ms",
    "p99_latency_ms",
]

COLUMNS = [
    ("Algorithm", "algorithm"),
    ("Backend", "backend"),
    ("Traffic", "traffic"),
    ("Concurrency", "concurrency"),
    ("Requests", "requests"),
    ("Allowed", "allowed"),
    ("Rejected", "rejected"),
    ("RPS", "rps"),
    ("Avg ms", "avg_latency_ms"),
    ("P50 ms", "p50_latency_ms"),
    ("P95 ms", "p95_latency_ms"),
    ("P99 ms", "p99_latency_ms"),
]


def to_table(results: list[dict]) -> str:
    """Render results as an aligned pipe-separated table."""
    if not results:
        return "No benchmark results."

    headers = [label for label, _ in COLUMNS]
    widths = {label: len(label) for label, _ in COLUMNS}

    rows = []
    for result in results:
        row = [str(result[key]) for _, key in COLUMNS]
        for (label, _), value in zip(COLUMNS, row):
            widths[label] = max(widths[label], len(value))
        rows.append(row)

    lines = []

    header_line = "| " + " | ".join(
        label.ljust(widths[label]) for label, _ in COLUMNS
    ) + " |"
    lines.append(header_line)
    lines.append("|" + "|".join("-" * (width + 2) for width in widths.values()) + "|")

    for row in rows:
        lines.append(
            "| " + " | ".join(
                value.ljust(widths[label])
                for (label, _), value in zip(COLUMNS, row)
            ) + " |"
        )

    return "\n".join(lines)


def to_csv(results: list[dict]) -> str:
    """Serialize results as CSV text."""
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=FIELDS, extrasaction="ignore")
    writer.writeheader()
    for result in results:
        writer.writerow(result)
    return buffer.getvalue()


def to_json(results: list[dict]) -> str:
    """Serialize results as JSON text."""
    return json.dumps(results, indent=2)


def save_results(
    results: list[dict],
    output_dir: str,
    fmt: str = "all",
) -> dict:
    """Write CSV/JSON result files into ``output_dir``.

    Returns a mapping of format -> written path.
    """
    os.makedirs(output_dir, exist_ok=True)

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")

    paths = {}

    if fmt in ("csv", "all"):
        path = os.path.join(
            output_dir,
            f"rateguard-benchmark-{timestamp}.csv",
        )
        with open(path, "w", newline="", encoding="utf-8") as handle:
            handle.write(to_csv(results))
        paths["csv"] = path

    if fmt in ("json", "all"):
        path = os.path.join(
            output_dir,
            f"rateguard-benchmark-{timestamp}.json",
        )
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(to_json(results))
        paths["json"] = path

    return paths