"""Prometheus-compatible metrics for RateGuard.

Metrics are **process-local** by default: every uvicorn worker counts its
own requests in memory and ``/metrics`` exposes that worker's view. When
``PROMETHEUS_MULTIPROC_DIR`` is set (as in docker-compose), the standard
``prometheus_client`` multiprocess mode aggregates the workers of one
container at scrape time — no Redis writes, no per-request overhead.

Labels are strictly bounded. Routes are only labelled when they are known
(configured or built-in); anything else collapses into the single value
``other``. API keys, client identities and IP addresses are never used as
labels.
"""

import os

from prometheus_client import (
    CONTENT_TYPE_LATEST,
    REGISTRY,
    CollectorRegistry,
    Counter,
    Histogram,
    generate_latest,
)


def _multiprocess_enabled():
    """Prepare multiprocess mode when requested via the environment.

    Must run before any metric object is created so that
    ``prometheus_client`` switches its value classes over.
    """
    for var in ("PROMETHEUS_MULTIPROC_DIR", "prometheus_multiproc_dir"):
        directory = os.environ.get(var)

        if directory:
            os.makedirs(directory, exist_ok=True)
            return True

    return False


MULTIPROCESS_ENABLED = _multiprocess_enabled()


# Built-in routes that always get their own label value. Anything not in
# here (and not passed as a configured route) is reported as "other".
KNOWN_ROUTES = frozenset(
    {
        "/",
        "/metrics",
        "/api/test",
        "/api/login",
        "/api/products",
        "/api/orders",
    }
)


HTTP_REQUESTS_TOTAL = Counter(
    "rateguard_http_requests_total",
    "Total HTTP requests processed.",
    ["route", "status"],
)

RATE_LIMIT_REQUESTS_TOTAL = Counter(
    "rateguard_rate_limit_requests_total",
    "Rate-limit decisions (allowed vs rejected).",
    ["decision", "algorithm", "backend", "route"],
)

REQUEST_DURATION_SECONDS = Histogram(
    "rateguard_http_request_duration_seconds",
    "HTTP request latency in seconds, measured by the ASGI middleware.",
    ["route"],
    buckets=(
        0.005,
        0.01,
        0.025,
        0.05,
        0.1,
        0.25,
        0.5,
        1.0,
        2.5,
        5.0,
    ),
)


def registry():
    """Return the collector registry to scrape.

    In multiprocess mode a fresh ``CollectorRegistry`` with a
    ``MultiProcessCollector`` merges all worker files; otherwise the
    default process-local registry is used.
    """
    if MULTIPROCESS_ENABLED:
        from prometheus_client import multiprocess

        collector_registry = CollectorRegistry()
        multiprocess.MultiProcessCollector(collector_registry)
        return collector_registry

    return REGISTRY


def metrics_body() -> bytes:
    """Render the Prometheus text exposition format."""
    return generate_latest(registry())


def route_label(path, known_routes=None) -> str:
    """Bound the route label cardinality to known routes."""
    if path in KNOWN_ROUTES:
        return path

    if known_routes and path in known_routes:
        return path

    return "other"


def record_http_request(route: str, status) -> None:
    HTTP_REQUESTS_TOTAL.labels(
        route=route,
        status=str(status),
    ).inc()


def record_rate_limit_decision(
    allowed: bool,
    algorithm: str,
    backend: str,
    route: str,
) -> None:
    RATE_LIMIT_REQUESTS_TOTAL.labels(
        decision="allowed" if allowed else "rejected",
        algorithm=algorithm,
        backend=backend,
        route=route,
    ).inc()


def observe_latency(route: str, seconds: float) -> None:
    REQUEST_DURATION_SECONDS.labels(route=route).observe(seconds)
