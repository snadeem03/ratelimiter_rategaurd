"""Tests for the benchmark SVG chart generator."""

import pytest

from benchmarks.chart import extract_rows, main, render_svg

ENVIRONMENT = {
    "python": "3.12.0",
    "os": "Linux 6.1",
    "cpu_count": 4,
    "redis_version": "7.2.4",
}


def sample_report():
    return {
        "environment": ENVIRONMENT,
        "results": [
            {"backend": "memory", "algorithm": "token_bucket",
             "throughput_rps": 1000.5},
            {"backend": "memory", "algorithm": "fixed_window",
             "throughput_rps": 900.25},
            {"backend": "redis", "algorithm": "fixed_window",
             "throughput_rps": 10.125},
        ],
    }


class TestExtractRows:
    def test_rows_carry_backend_algorithm_throughput(self):
        rows = extract_rows(sample_report())
        assert rows == [
            {"backend": "memory", "algorithm": "token_bucket",
             "throughput": 1000.5},
            {"backend": "memory", "algorithm": "fixed_window",
             "throughput": 900.25},
            {"backend": "redis", "algorithm": "fixed_window",
             "throughput": 10.125},
        ]


class TestRenderSvg:
    def test_contains_every_value_and_label(self):
        svg = render_svg(extract_rows(sample_report()), ENVIRONMENT)
        for fragment in (
            "token_bucket", "fixed_window",
            "1,000.5", "900.2", "10.1",
            "MEMORY", "REDIS", "req/s",
            "Python 3.12.0", "Linux 6.1", "Redis 7.2.4",
            "environment-dependent",
        ):
            assert fragment in svg

    def test_rendering_is_deterministic(self):
        first = render_svg(extract_rows(sample_report()), ENVIRONMENT)
        second = render_svg(extract_rows(sample_report()), ENVIRONMENT)
        assert first == second

    def test_valid_xml_document(self):
        import xml.etree.ElementTree as ET

        svg = render_svg(extract_rows(sample_report()), ENVIRONMENT)
        root = ET.fromstring(svg)
        assert root.tag.endswith("svg")

    def test_empty_results_rejected(self):
        with pytest.raises(ValueError):
            render_svg([], ENVIRONMENT)


class TestMain:
    def test_writes_svg_file(self, tmp_path):
        source = tmp_path / "report.json"
        source.write_text(
            __import__("json").dumps(sample_report()), encoding="utf-8"
        )
        target = tmp_path / "chart.svg"
        exit_code = main([str(source), str(target)])
        assert exit_code == 0
        assert target.read_text(encoding="utf-8").startswith("<svg")
