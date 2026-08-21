from typing import Dict


def parse_route_limits(raw: str) -> Dict[str, Dict[str, int]]:
    """Parse the RATE_LIMIT_ROUTES environment variable.

    Format: comma-separated "path:limit:window" entries, e.g.::

        /api/login:10:60,/api/products:200:60

    Returns a mapping of route path to ``{"limit": int, "window": int}``.
    Raises ``ValueError`` for malformed entries (fail-fast at startup):
    non-numeric or non-positive limits/windows are rejected.
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
                f"Path must start with '/'. Entry was: {entry!r}."
            )

        try:
            limit_value = int(limit)
            window_value = int(window)
        except ValueError:
            raise ValueError(
                f"Invalid RATE_LIMIT_ROUTES entry: {entry!r}. "
                "limit and window must be integers."
            ) from None

        if limit_value < 1 or window_value < 1:
            raise ValueError(
                f"Invalid RATE_LIMIT_ROUTES entry: {entry!r}. "
                "limit and window must be >= 1."
            )

        routes[path] = {
            "limit": limit_value,
            "window": window_value,
        }

    return routes
