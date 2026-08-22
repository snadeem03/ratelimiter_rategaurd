"""Internal representation of a runtime-managed rate-limit policy.

A ``RoutePolicy`` binds a route path to a request limit and window
length. Policies are validated on construction: malformed, zero or
negative values and unsafe route paths are rejected with ``ValueError``
so that invalid data can never reach a store or the limiter hot path.
"""

import re
from dataclasses import dataclass


# Conservative path charset: printable ASCII subset without whitespace,
# quotes, angle brackets, backslashes, braces (Redis Cluster hash tags),
# wildcards or percent/query/fragment characters. The first segment must
# be empty (i.e. the path starts with "/").
_ROUTE_RE = re.compile(r"^/[A-Za-z0-9._~!$&'()*+,;=:@/\-]*$")

MAX_ROUTE_LENGTH = 256

# Upper bounds keep values sane; the hard requirement is >= 1.
MAX_LIMIT = 1_000_000_000
MAX_WINDOW = 31_536_000  # one year, in seconds


def validate_route(route) -> str:
    """Validate a route path, returning it unchanged.

    Raises ``ValueError`` for anything that is not a safe, exact-match
    HTTP path: wrong type, missing leading "/", whitespace/control
    characters, "." or ".." segments, overlong paths.
    """
    if not isinstance(route, str):
        raise ValueError(
            f"route must be a string, got {type(route).__name__}"
        )

    candidate = route.strip()

    if not candidate:
        raise ValueError("route must not be empty")

    if len(candidate) > MAX_ROUTE_LENGTH:
        raise ValueError(
            f"route must be at most {MAX_ROUTE_LENGTH} characters"
        )

    if not _ROUTE_RE.match(candidate):
        raise ValueError(
            f"route {route!r} contains unsupported characters"
        )

    segments = [segment for segment in candidate.split("/") if segment]

    if not segments:
        raise ValueError(
            "route '/' is never rate-limited and cannot have a policy"
        )

    if any(segment in (".", "..") for segment in segments):
        raise ValueError(f"route {route!r} must not contain '.' or '..'")

    return candidate


def validate_limit(limit) -> int:
    """Validate a positive integer limit."""
    if isinstance(limit, bool) or not isinstance(limit, int):
        raise ValueError(
            f"limit must be an integer, got {limit!r}"
        )

    if limit < 1:
        raise ValueError(f"limit must be >= 1, got {limit}")

    if limit > MAX_LIMIT:
        raise ValueError(f"limit must be <= {MAX_LIMIT}, got {limit}")

    return limit


def validate_window(window) -> int:
    """Validate a positive integer window length in seconds."""
    if isinstance(window, bool) or not isinstance(window, int):
        raise ValueError(
            f"window must be an integer, got {window!r}"
        )

    if window < 1:
        raise ValueError(f"window must be >= 1, got {window}")

    if window > MAX_WINDOW:
        raise ValueError(f"window must be <= {MAX_WINDOW}, got {window}")

    return window


@dataclass(frozen=True)
class RoutePolicy:
    """A runtime-managed rate-limit policy for one route."""

    route: str
    limit: int
    window: int
    enabled: bool = True

    def __post_init__(self):
        object.__setattr__(
            self,
            "route",
            validate_route(self.route),
        )
        object.__setattr__(self, "limit", validate_limit(self.limit))
        object.__setattr__(self, "window", validate_window(self.window))

        if not isinstance(self.enabled, bool):
            raise ValueError(
                f"enabled must be a boolean, got {self.enabled!r}"
            )

    def to_dict(self) -> dict:
        return {
            "route": self.route,
            "limit": self.limit,
            "window": self.window,
            "enabled": self.enabled,
        }

    @classmethod
    def from_dict(cls, data) -> "RoutePolicy":
        """Build a policy from a plain mapping, validating every field.

        Unknown keys are ignored so stores can evolve their payload;
        missing required fields are rejected.
        """
        if not isinstance(data, dict):
            raise ValueError(
                f"policy must be an object, got {type(data).__name__}"
            )

        try:
            route = data["route"]
            limit = data["limit"]
            window = data["window"]
        except KeyError as exc:
            raise ValueError(
                f"policy is missing required field: {exc.args[0]!r}"
            ) from None

        enabled = data.get("enabled", True)

        if not isinstance(enabled, bool):
            raise ValueError(
                f"enabled must be a boolean, got {enabled!r}"
            )

        return cls(
            route=route,
            limit=limit,
            window=window,
            enabled=enabled,
        )


def normalize_policy_payload(payload) -> dict:
    """Normalize an API create/update body into store-friendly fields.

    Returns ``{"limit", "window", "enabled"}``. ``route`` is handled by
    the caller because it appears in the URL for updates. Rejects
    unknown/invalid types with ``ValueError``.
    """
    if not isinstance(payload, dict):
        raise ValueError("request body must be a JSON object")

    unknown = set(payload) - {"limit", "window", "enabled"}

    if unknown:
        raise ValueError(
            "unknown fields: " + ", ".join(sorted(unknown))
        )

    fields = {}

    if "limit" in payload:
        fields["limit"] = validate_limit(payload["limit"])

    if "window" in payload:
        fields["window"] = validate_window(payload["window"])

    if "enabled" in payload:
        if not isinstance(payload["enabled"], bool):
            raise ValueError(
                f"enabled must be a boolean, got {payload['enabled']!r}"
            )

        fields["enabled"] = payload["enabled"]

    return fields
