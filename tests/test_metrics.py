import re
import uuid

import pytest
from fastapi.testclient import TestClient
from prometheus_client import REGISTRY
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route

from app import main as app_module
from app.main import app
from app.middleware.rate_limiter import RateLimiter
from app.middleware.rate_limit_middleware import RateLimitMiddleware


METRIC_NAMES = {
    "rateguard_http_requests_total",
    "rateguard_rate_limit_requests_total",
    "rateguard_http_request_duration_seconds",
    "rateguard_rate_limit_utilization",
}

ALLOWED_LABELS = {"route", "status", "decision", "algorithm", "backend"}

FORBIDDEN_LABELS = {
    "client",
    "client_id",
    "clientid",
    "ip",
    "api_key",
    "apikey",
    "key",
    "owner",
    "id",
    "session",
}


def _sample(name, **labels):
    return REGISTRY.get_sample_value(name, labels)


def _delta(name, labels):
    """Return (before, after-capturing) helper for a labelled sample."""
    before = _sample(name, **labels)

    def after():
        current = _sample(name, **labels)
        return (current or 0) - (before or 0)

    return after


def _unique_client():
    # Opaque (non-managed) API key -> its own quota, isolated from other
    # tests and never a managed rg_live_ key.
    return f"metrics-test-{uuid.uuid4().hex}"


def _request(path, http_client, client_key=None):
    headers = {"X-API-Key": client_key} if client_key else {}
    return http_client.get(path, headers=headers)


@pytest.fixture()
def client():
    return TestClient(app)


async def _echo(request):
    return JSONResponse({"ok": True})


def _mini_app():
    return Starlette(
        routes=[
            Route("/api/test", _echo),
            Route("/other/route", _echo),
        ]
    )


def _limited_app(
    limiter,
    known_routes=frozenset({"/api/test", "/other/route"}),
    client_key=None,
):
    identity = client_key or f"unit-{uuid.uuid4().hex}"

    return RateLimitMiddleware(
        _mini_app(),
        client_key_fn=lambda request: identity,
        get_rate_limiter=lambda: limiter,
        excluded_paths={"/"},
        known_routes=known_routes,
    )


class TestMetricsEndpoint:
    def test_metrics_returns_200(self, client):
        response = client.get("/metrics")

        assert response.status_code == 200
        assert "text/plain" in response.headers["Content-Type"]

    def test_metrics_not_rate_limited(self, client):
        for _ in range(app_module.limit + 5):
            assert client.get("/metrics").status_code == 200

    def test_metrics_does_not_consume_budget(self, client):
        for _ in range(app_module.limit + 3):
            client.get("/metrics")

        key = _unique_client()

        for _ in range(app_module.limit):
            assert _request("/api/test", client, key).status_code == 200

    def test_expected_metric_names_present(self, client):
        body = client.get("/metrics").text

        for name in METRIC_NAMES:
            assert name in body

        for name in (
            "rateguard_http_request_duration_seconds_count",
            "rateguard_http_request_duration_seconds_sum",
            "rateguard_http_request_duration_seconds_bucket",
        ):
            assert name in body

    def test_latency_recorded(self, client):
        route = "/api/test"
        before = (
            _sample(
                "rateguard_http_request_duration_seconds_count",
                route=route,
            )
            or 0
        )

        _request(route, client, _unique_client())

        after = (
            _sample(
                "rateguard_http_request_duration_seconds_count",
                route=route,
            )
            or 0
        )

        assert after > before

    def test_latency_sum_is_positive_after_requests(self, client):
        _request("/api/test", client, _unique_client())

        total = _sample(
            "rateguard_http_request_duration_seconds_sum",
            route="/api/test",
        )

        assert total >= 0

    def test_registry_is_process_local_by_default(self):
        from app.metrics import MULTIPROCESS_ENABLED, registry

        if MULTIPROCESS_ENABLED:
            pytest.skip("multiprocess mode enabled in this environment")

        assert registry() is REGISTRY


class TestCounters:
    def test_allowed_requests_increment_counter(self, client):
        key = _unique_client()
        allowed_delta = _delta(
            "rateguard_rate_limit_requests_total",
            {
                "decision": "allowed",
                "algorithm": app_module.algorithm,
                "backend": "memory"
                if app_module.RATE_LIMIT_BACKEND == "memory"
                else "redis",
                "route": "/api/test",
            },
        )

        response = _request("/api/test", client, key)

        assert response.status_code == 200
        assert allowed_delta() == 1

    def test_rejected_429_increments_rejection(self, client):
        key = _unique_client()
        rejected_delta = _delta(
            "rateguard_rate_limit_requests_total",
            {
                "decision": "rejected",
                "algorithm": app_module.algorithm,
                "backend": "memory"
                if app_module.RATE_LIMIT_BACKEND == "memory"
                else "redis",
                "route": "/api/test",
            },
        )

        statuses = [
            _request("/api/test", client, key).status_code
            for _ in range(app_module.limit + 1)
        ]

        assert 429 in statuses
        assert rejected_delta() >= 1

    def test_algorithm_label_matches_configured_algorithm(self, client):
        key = _unique_client()
        wrong_algorithm_delta = _delta(
            "rateguard_rate_limit_requests_total",
            {
                "decision": "allowed",
                "algorithm": "definitely-not-an-algorithm",
                "backend": "memory",
                "route": "/api/test",
            },
        )

        _request("/api/test", client, key)

        assert wrong_algorithm_delta() == 0

        correct = _sample(
            "rateguard_rate_limit_requests_total",
            **{
                "decision": "allowed",
                "algorithm": app_module.algorithm,
                "backend": "memory"
                if app_module.RATE_LIMIT_BACKEND == "memory"
                else "redis",
                "route": "/api/test",
            },
        )

        assert correct is not None

    def test_status_label_records_http_status(self, client):
        key = _unique_client()
        ok_delta = _delta(
            "rateguard_http_requests_total",
            {"route": "/api/test", "status": "200"},
        )

        assert _request("/api/test", client, key).status_code == 200
        assert ok_delta() == 1

    def test_unknown_route_aggregates_under_other(self, client):
        other_delta = _delta(
            "rateguard_http_requests_total",
            {"route": "other", "status": "404"},
        )

        assert client.get("/does-not-exist").status_code == 404
        assert other_delta() == 1

    def test_excluded_path_still_counted_as_http_request(self, client):
        root_delta = _delta(
            "rateguard_http_requests_total",
            {"route": "/", "status": "200"},
        )

        assert client.get("/").status_code == 200
        assert root_delta() == 1


class TestUtilizationMetric:
    def _utilization(self, route="/api/test"):
        return REGISTRY.get_sample_value(
            "rateguard_rate_limit_utilization",
            {"route": route},
        )

    def test_partial_utilization_after_first_request(self, client):
        key = _unique_client()

        assert _request("/api/test", client, key).status_code == 200

        expected = 1 - (app_module.limit - 1) / app_module.limit

        assert self._utilization() == pytest.approx(expected)

    def test_full_utilization_when_budget_exhausted(self, client):
        key = _unique_client()
        statuses = [
            _request("/api/test", client, key).status_code
            for _ in range(app_module.limit + 2)
        ]

        assert 429 in statuses
        assert self._utilization() == pytest.approx(1.0)

    def test_utilization_stays_within_unit_range(self, client):
        key = _unique_client()

        for _ in range(app_module.limit + 3):
            _request("/api/test", client, key)

        value = self._utilization()

        assert 0.0 <= value <= 1.0


class TestBackendLabels:
    def test_memory_backend_label(self):
        limiter = RateLimiter(limit=3, window=60, algorithm="token_bucket")
        middleware_client = TestClient(_limited_app(limiter))
        allowed_delta = _delta(
            "rateguard_rate_limit_requests_total",
            {
                "decision": "allowed",
                "algorithm": "token_bucket",
                "backend": "memory",
                "route": "/api/test",
            },
        )

        assert middleware_client.get("/api/test").status_code == 200
        assert allowed_delta() == 1

    def test_all_algorithms_report_their_name(self):
        for algorithm in (
            "fixed_window",
            "sliding_window",
            "token_bucket",
            "leaky_bucket",
        ):
            limiter = RateLimiter(limit=2, window=60, algorithm=algorithm)
            middleware_client = TestClient(_limited_app(limiter))
            allowed_delta = _delta(
                "rateguard_rate_limit_requests_total",
                {
                    "decision": "allowed",
                    "algorithm": algorithm,
                    "backend": "memory",
                    "route": "/api/test",
                },
            )

            assert middleware_client.get("/api/test").status_code == 200
            assert allowed_delta() == 1

    def test_rejected_label_on_unit_middleware(self):
        limiter = RateLimiter(limit=1, window=60, algorithm="leaky_bucket")
        middleware_client = TestClient(_limited_app(limiter))
        rejected_delta = _delta(
            "rateguard_rate_limit_requests_total",
            {
                "decision": "rejected",
                "algorithm": "leaky_bucket",
                "backend": "memory",
                "route": "/api/test",
            },
        )

        middleware_client.get("/api/test")
        assert middleware_client.get("/api/test").status_code == 429
        assert rejected_delta() == 1


class TestRedisBackendMetrics:
    def _require_redis(self):
        try:
            from app.core.redis_client import get_redis

            get_redis().ping()
        except Exception:
            pytest.skip("Redis is not available")

    def test_redis_backend_label(self):
        self._require_redis()

        from app.core.redis_client import get_redis
        from app.storage.redis_storage import RedisStorage

        marker = uuid.uuid4().hex
        limiter = RateLimiter(
            limit=2,
            window=60,
            algorithm="fixed_window",
            storage=RedisStorage(get_redis()),
        )
        middleware_client = TestClient(
            RateLimitMiddleware(
                _mini_app(),
                client_key_fn=lambda request: f"redis-metrics-{marker}",
                get_rate_limiter=lambda: limiter,
                excluded_paths={"/"},
                known_routes=frozenset({"/api/test"}),
            )
        )
        redis_delta = _delta(
            "rateguard_rate_limit_requests_total",
            {
                "decision": "allowed",
                "algorithm": "fixed_window",
                "backend": "redis",
                "route": "/api/test",
            },
        )
        rejected_delta = _delta(
            "rateguard_rate_limit_requests_total",
            {
                "decision": "rejected",
                "algorithm": "fixed_window",
                "backend": "redis",
                "route": "/api/test",
            },
        )

        try:
            assert middleware_client.get("/api/test").status_code == 200
            assert middleware_client.get("/api/test").status_code == 200
            assert middleware_client.get("/api/test").status_code == 429
        finally:
            for found in get_redis().scan_iter(f"*{marker}*"):
                get_redis().delete(found)

        assert redis_delta() == 2
        assert rejected_delta() == 1


class TestCardinalityAndSecurity:
    def test_only_bounded_label_names_used(self, client):
        body = client.get("/metrics").text

        label_names = set()
        for line in body.splitlines():
            if not line.startswith("rateguard_"):
                continue

            match = re.match(r"^[^\s{]+\{([^}]*)\}", line)

            if not match:
                continue

            for pair in match.group(1).split('",'):
                name = pair.split("=", 1)[0].strip('"')
                label_names.add(name)

        assert label_names <= ALLOWED_LABELS | {"le"}

    def test_no_forbidden_labels_anywhere(self, client):
        body = client.get("/metrics").text.lower()

        for forbidden in FORBIDDEN_LABELS:
            assert f'{forbidden}="' not in body

    def test_no_secrets_or_keys_in_label_values(self, client):
        secret_like = _unique_client()
        _request("/api/test", client, secret_like)

        body = client.get("/metrics").text

        assert "rg_live_" not in body
        assert secret_like not in body
        assert not re.search(r'(route|status|algorithm|backend|decision)="(\d{1,3}\.){3}\d{1,3}"', body)

    def test_route_label_values_are_known_or_other(self, client):
        body = client.get("/metrics").text

        route_values = set(re.findall(r'route="([^"]*)"', body))

        assert route_values <= app_module.METRICS_KNOWN_ROUTES | {"other"}

