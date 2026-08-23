"""Generate a static SVG throughput chart from a benchmark JSON export.

Standard library only. The SVG is committed to the repository; the JSON
export it was generated from is not.

Usage:
    python -m benchmarks.chart <benchmark-results.json> [output.svg]
"""

import json
import sys

MEMORY_COLOR = "#3fb950"
REDIS_COLOR = "#58a6ff"
TEXT_COLOR = "#c9d1d9"
MUTED_COLOR = "#8b949e"
BG_COLOR = "#0d1117"

CHART_WIDTH = 720
ROW_HEIGHT = 34
BAR_HEIGHT = 22
PADDING = 16
TITLE_HEIGHT = 46
CAPTION_LINES = 2


def _escape(text: str) -> str:
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def extract_rows(report: dict) -> list:
    rows = []
    for result in report["results"]:
        rows.append({
            "backend": result["backend"],
            "algorithm": result["algorithm"],
            "throughput": result["throughput_rps"],
        })
    return rows


def render_svg(rows: list, environment: dict) -> str:
    backends = []
    for backend in ("memory", "redis"):
        backend_rows = sorted(
            (r for r in rows if r["backend"] == backend),
            key=lambda r: r["algorithm"],
        )
        if backend_rows:
            backends.append((backend, backend_rows))
    if not backends:
        raise ValueError("no benchmark results to chart")

    label_x = 130
    value_pad = 8
    bar_max_width = CHART_WIDTH - label_x - 110
    body_height = len(backends) * (PADDING + ROW_HEIGHT * 4 + PADDING)
    height = TITLE_HEIGHT + body_height + CAPTION_LINES * 18 + PADDING

    lines = [
        f'<svg xmlns="http://www.w3.org/2000/svg" '
        f'width="{CHART_WIDTH}" height="{height}" '
        f'viewBox="0 0 {CHART_WIDTH} {height}" role="img" '
        f'aria-label="RateGuard benchmark throughput by algorithm and '
        f'backend">',
        f'<rect width="{CHART_WIDTH}" height="{height}" '
        f'fill="{BG_COLOR}"/>',
        f'<text x="{PADDING}" y="28" fill="{TEXT_COLOR}" '
        f'font-family="Segoe UI, Arial, sans-serif" font-size="17" '
        f'font-weight="600">Benchmark snapshot — throughput (req/s)'
        f'</text>',
        f'<text x="{PADDING}" y="44" fill="{MUTED_COLOR}" '
        f'font-family="Segoe UI, Arial, sans-serif" font-size="11">'
        f'{_escape(_env_line(environment))}</text>',
    ]

    y = TITLE_HEIGHT
    for backend, backend_rows in backends:
        peak = max(r["throughput"] for r in backend_rows)
        color = MEMORY_COLOR if backend == "memory" else REDIS_COLOR
        y += PADDING
        lines.append(
            f'<text x="{PADDING}" y="{y + 4}" fill="{color}" '
            f'font-family="Consolas, monospace" font-size="12" '
            f'font-weight="700">{_escape(backend.upper())}</text>'
        )
        for row in backend_rows:
            y += ROW_HEIGHT
            bar_width = max(
                2, round(row["throughput"] / peak * bar_max_width)
            )
            lines.append(
                f'<text x="{PADDING}" y="{y + BAR_HEIGHT - 6}" '
                f'fill="{TEXT_COLOR}" font-family="Consolas, monospace" '
                f'font-size="12">{_escape(row["algorithm"])}</text>'
            )
            lines.append(
                f'<rect x="{label_x}" y="{y}" width="{bar_width}" '
                f'height="{BAR_HEIGHT}" rx="3" fill="{color}"/>'
            )
            lines.append(
                f'<text x="{label_x + bar_width + value_pad}" '
                f'y="{y + BAR_HEIGHT - 6}" fill="{TEXT_COLOR}" '
                f'font-family="Consolas, monospace" font-size="12">'
                f'{row["throughput"]:,.1f}</text>'
            )
        y += PADDING

    caption_y = height - CAPTION_LINES * 18 + 2
    lines.append(
        f'<text x="{PADDING}" y="{caption_y}" fill="{MUTED_COLOR}" '
        f'font-family="Segoe UI, Arial, sans-serif" font-size="11">'
        f'Bars are scaled within each backend panel; values are printed '
        f'on each bar.</text>'
    )
    lines.append(
        f'<text x="{PADDING}" y="{caption_y + 18}" fill="{MUTED_COLOR}" '
        f'font-family="Segoe UI, Arial, sans-serif" font-size="11">'
        f'Results are environment-dependent — measurements of one '
        f'machine, not universal guarantees.</text>'
    )
    lines.append("</svg>")
    return "\n".join(lines) + "\n"


def _env_line(environment: dict) -> str:
    parts = [
        f"Python {environment.get('python', '?')}",
        str(environment.get("os", "")),
    ]
    if environment.get("redis_version"):
        parts.append(f"Redis {environment['redis_version']}")
    if environment.get("cpu_count"):
        parts.append(f"{environment['cpu_count']} cores")
    return " · ".join(part for part in parts if part)


def main(argv=None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    if len(argv) not in (1, 2):
        print(
            "usage: python -m benchmarks.chart "
            "<benchmark-results.json> [output.svg]",
            file=sys.stderr,
        )
        return 2
    with open(argv[0], encoding="utf-8") as handle:
        report = json.load(handle)
    svg = render_svg(extract_rows(report), report.get("environment", {}))
    output = argv[1] if len(argv) == 2 else "benchmark-throughput.svg"
    with open(output, "w", encoding="utf-8") as handle:
        handle.write(svg)
    print(f"Chart written to {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
