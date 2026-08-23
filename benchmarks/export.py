"""JSON result export for the benchmark suite."""

import json
import os
import platform
from datetime import datetime, timezone

_LATENCY_FIELD_MAP = {
    "avg_ms": "avg_latency_ms",
    "p50_ms": "p50_latency_ms",
    "p95_ms": "p95_latency_ms",
    "p99_ms": "p99_latency_ms",
}


def collect_environment(redis_client=None) -> dict:
    environment = {
        "python": platform.python_version(),
        "os": f"{platform.system()} {platform.release()}",
        "cpu_count": os.cpu_count(),
    }
    processor = platform.processor()
    if processor:
        environment["cpu"] = processor
    if redis_client is not None:
        try:
            environment["redis_version"] = redis_client.info("server")[
                "redis_version"
            ]
        except Exception:
            pass
    return environment


def _map_result(result: dict) -> dict:
    mapped = {
        key: value
        for key, value in result.items()
        if key not in ("concurrency", "run_id")
    }
    for old, new in _LATENCY_FIELD_MAP.items():
        if old in mapped:
            mapped[new] = mapped.pop(old)
    return mapped


def build_report(configuration: dict, results, redis_client=None) -> dict:
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "environment": collect_environment(redis_client),
        "configuration": configuration,
        "results": [_map_result(r) for r in results],
    }


def write_report(path: str, report: dict) -> None:
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(report, handle, indent=2)
        handle.write("\n")
