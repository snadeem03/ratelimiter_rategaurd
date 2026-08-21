"""Configuration and integration tests for the observability stack.

These tests validate the static configuration shipped with the repository
(docker-compose.yml, prometheus/prometheus.yml, Grafana provisioning) and
cross-check that every dashboard query uses a metric that RateGuard
actually exposes at ``GET /metrics``. No timing or rendering assertions.
"""

import json
import re
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from app.main import app


BASE_DIR = Path(__file__).resolve().parents[1]
COMPOSE_FILE = BASE_DIR / "docker-compose.yml"
PROMETHEUS_FILE = BASE_DIR / "prometheus" / "prometheus.yml"
DATASOURCE_FILE = (
    BASE_DIR / "grafana" / "provisioning" / "datasources" / "prometheus.yml"
)
DASHBOARD_PROVIDER_FILE = (
    BASE_DIR / "grafana" / "provisioning" / "dashboards" / "provider.yml"
)

DASHBOARD_FILES = [
    path
    for path in (
        BASE_DIR / "grafana" / "provisioning" / "dashboards"
    ).glob("*.json")
]

FORBIDDEN_QUERY_TERMS = re.compile(
    r"\b(client|client_id|ip|api_key|apikey|owner|session|key)\b",
    re.IGNORECASE,
)


@pytest.fixture(scope="module")
def compose():
    with COMPOSE_FILE.open(encoding="utf-8") as handle:
        return yaml.safe_load(handle)


@pytest.fixture(scope="module")
def client():
    return TestClient(app)


@pytest.fixture(scope="module")
def prometheus_config():
    with PROMETHEUS_FILE.open(encoding="utf-8") as handle:
        return yaml.safe_load(handle)


@pytest.fixture(scope="module")
def datasource():
    with DATASOURCE_FILE.open(encoding="utf-8") as handle:
        document = yaml.safe_load(handle)

    return document["datasources"][0]


def _service(compose, name):
    return compose["services"][name]


class TestComposeServices:
    def test_all_four_services_defined(self, compose):
        assert set(_service(compose, name) is not None for name in (
            "rategaurd",
            "redis",
            "prometheus",
            "grafana",
        )) == {True}

    def test_prometheus_scrapes_rategaurd_over_internal_network(
        self, compose
    ):
        prometheus = _service(compose, "prometheus")

        assert any(
            part.startswith("--config.file=")
            for part in prometheus["command"]
        )
        assert not prometheus.get("ports"), (
            "Prometheus must not be published to the host"
        )

    def test_redis_remains_internal(self, compose):
        assert not _service(compose, "redis").get("ports")

    def test_only_expected_host_ports_published(self, compose):
        published = {
            name: _service(compose, name).get("ports") or []
            for name in ("rategaurd", "redis", "prometheus", "grafana")
        }

        assert published["rategaurd"] == ["8000:8000"]
        assert published["redis"] == []
        assert published["prometheus"] == []
        assert published["grafana"] == ["3000:3000"]

    def test_named_volumes_declared_and_mounted(self, compose):
        volumes = compose.get("volumes") or {}

        assert "prometheus_data" in volumes
        assert "grafana_data" in volumes

        prometheus_sources = [
            mount.split(":")[0]
            for mount in _service(compose, "prometheus")["volumes"]
        ]
        grafana_sources = [
            mount.split(":")[0]
            for mount in _service(compose, "grafana")["volumes"]
        ]

        assert "prometheus_data" in prometheus_sources
        assert "grafana_data" in grafana_sources

    def test_grafana_provisioning_directory_mounted_readonly(self, compose):
        mounts = _service(compose, "grafana")["volumes"]

        assert any(
            mount.endswith(":/etc/grafana/provisioning:ro")
            for mount in mounts
        )

    def test_grafana_credentials_come_from_environment(self, compose):
        environment = _service(compose, "grafana")["environment"]

        assert environment["GF_SECURITY_ADMIN_USER"].startswith(
            "${GRAFANA_ADMIN_USER"
        )
        assert environment["GF_SECURITY_ADMIN_PASSWORD"].startswith(
            "${GRAFANA_ADMIN_PASSWORD"
        )

    def test_health_checks_present_for_every_service(self, compose):
        for name in ("rategaurd", "redis", "prometheus", "grafana"):
            assert _service(compose, name).get("healthcheck"), (
                f"{name} is missing a healthcheck"
            )


class TestPrometheusConfig:
    def test_rategaurd_job_targets_service_name(self, prometheus_config):
        jobs = {
            job["job_name"]: job
            for job in prometheus_config["scrape_configs"]
        }

        assert "rategaurd" in jobs

        job = jobs["rategaurd"]

        assert job["metrics_path"] == "/metrics"

        targets = [
            target
            for group in job["static_configs"]
            for target in group["targets"]
        ]

        assert "rategaurd:8000" in targets

    def test_scrape_interval_configured(self, prometheus_config):
        assert prometheus_config["global"]["scrape_interval"]


class TestGrafanaProvisioning:
    def test_datasource_points_at_prometheus_service(self, datasource):
        assert datasource["type"] == "prometheus"
        assert datasource["url"] == "http://prometheus:9090"
        assert datasource["access"] == "proxy"
        assert datasource["isDefault"] is True

    def test_datasource_uid_is_stable(self, datasource):
        assert datasource["uid"] == "prometheus"

    def test_dashboard_provider_scans_provisioning_directory(self):
        with DASHBOARD_PROVIDER_FILE.open(encoding="utf-8") as handle:
            document = yaml.safe_load(handle)

        provider = document["providers"][0]

        assert provider["type"] == "file"
        assert (
            provider["options"]["path"]
            == "/etc/grafana/provisioning/dashboards"
        )


def _dashboard_metric_names(expression):
    return set(re.findall(r"rateguard_[a-zA-Z0-9_:]+", expression))


def _exposed_metric_names(body):
    """Metric family names actually exposed by GET /metrics."""
    families = set()

    for line in body.splitlines():
        if line.startswith("# TYPE "):
            families.add(line.split()[2])

    expanded = set(families)

    for family in families:
        expanded.add(f"{family}_bucket")
        expanded.add(f"{family}_sum")
        expanded.add(f"{family}_count")

    return expanded


@pytest.fixture(scope="module")
def dashboards():
    assert DASHBOARD_FILES, "no provisioned dashboard JSON found"

    loaded = []

    for path in DASHBOARD_FILES:
        with path.open(encoding="utf-8") as handle:
            loaded.append(json.load(handle))

    return loaded


class TestDashboard:
    def test_dashboards_have_panels_with_promql_queries(self, dashboards):
        for dashboard in dashboards:
            assert dashboard.get("title")
            assert dashboard.get("panels")

            for panel in dashboard["panels"]:
                assert panel.get("targets"), (
                    f"panel {panel.get('title')} has no queries"
                )

                for target in panel["targets"]:
                    assert target.get("expr"), (
                        f"panel {panel.get('title')} has an empty query"
                    )

    def test_queries_reference_the_provisioned_datasource(self, dashboards):
        for dashboard in dashboards:
            for panel in dashboard["panels"]:
                assert panel["datasource"]["uid"] == "prometheus"

    def test_every_query_uses_a_real_rateguard_metric(
        self, dashboards, client
    ):
        exposed = _exposed_metric_names(client.get("/metrics").text)

        for dashboard in dashboards:
            for panel in dashboard["panels"]:
                for target in panel["targets"]:
                    for name in _dashboard_metric_names(target["expr"]):
                        assert name in exposed, (
                            f"{name} is queried by "
                            f"'{panel.get('title')}' but never exposed "
                            "by RateGuard"
                        )

    def test_no_high_cardinality_terms_in_queries(self, dashboards):
        for dashboard in dashboards:
            for panel in dashboard["panels"]:
                for target in panel["targets"]:
                    assert not FORBIDDEN_QUERY_TERMS.search(
                        target["expr"]
                    ), (
                        f"panel '{panel.get('title')}' references a "
                        "high-cardinality term"
                    )

    def test_required_panel_topics_are_covered(self, dashboards):
        combined = " ".join(
            target["expr"]
            for dashboard in dashboards
            for panel in dashboard["panels"]
            for target in panel["targets"]
        ).lower()

        # Total requests.
        assert "rateguard_http_requests_total" in combined
        # Allowed requests.
        assert 'decision="allowed"' in combined
        # Rejected requests / 429s.
        assert 'decision="rejected"' in combined
        # Rejection rate divides rejected by total decisions.
        assert "clamp_min(sum(rateguard_rate_limit_requests_total)" in combined
        # Requests by route / algorithm / backend.
        assert "by (route)" in combined
        assert "by (algorithm)" in combined
        assert "by (backend)" in combined
        # Request latency histogram.
        assert "histogram_quantile" in combined
        assert "rateguard_http_request_duration_seconds_bucket" in combined
        # Rate-limit utilization.
        assert "rateguard_rate_limit_utilization" in combined
