from typing import Dict


def parse_route_limits(raw: str) -> Dict[str, Dict[str, int]]:
    """Parse the RATE_LIMIT_ROUTES environment variable.

    Format: comma-separated "path:limit:window" entries, e.g.::

        /api/login:10:60,/api/products:200:60

    Returns a mapping of route path to ``{"limit": int, "window": int}``.
    Raises ``ValueError`` for malformed entries (fail-fast at startup).
    """
    if not raw:
        return {}

    routes: Dict[str, Dict[str, int]] = {}

    for entry in raw.split(","):
        entry = entry.strip()

        if not entry:
            continue

        parts = [part.strip() for part in entry.split(":")]

        if len(parts) != 3:
            raise ValueError(
                f"Invalid RATE_LIMIT_ROUTES entry: {entry!r}. "
                "Expected 'path:limit:window'."
            )

        path, limit, window = parts

        if not path.startswith("/"):
            raise ValueError(
                f"Invalid route path in RATE_LIMIT_ROUTES: {path!r}. "
                "Path must start with '/'. Entry was: {entry!r}."
            )

        routes[path] = {
            "limit": int(limit),
            "window": int(window),
        }

    return routes
