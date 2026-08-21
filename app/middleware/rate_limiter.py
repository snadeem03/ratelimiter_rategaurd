import threading
import time
from collections import OrderedDict

from app.algorithms.factory import create_rate_limiter


class RateLimiter:
    """
    Thread-safe, per-client rate limiter facade.

    Holds one algorithm instance per (route, client) key pair, so each
    client is limited independently per route. Idle keys are evicted
    (LRU + TTL) so memory stays bounded even with a very large number of
    distinct clients.
    """

    def __init__(
        self,
        limit: int = 5,
        window: int = 60,
        algorithm: str = "sliding_window",
        max_keys: int = 10_000,
        key_ttl: float = 3_600,
        storage=None,
        route_limits: dict = None,
    ):
        self.limit = limit
        self.window = window
        self.algorithm = algorithm
        self.max_keys = max_keys
        self.key_ttl = key_ttl
        self._storage = storage
        self._route_limits = route_limits or {}

        # internal key -> (limiter, last_seen_monotonic)
        self._limiters = OrderedDict()
        self._lock = threading.Lock()

    @property
    def backend(self) -> str:
        """The backing store: ``redis`` when shared storage is configured,
        otherwise ``memory``."""
        return "redis" if self._storage is not None else "memory"

    def _route_config(self, route):
        """Resolve (limit, window) for a route, falling back to the
        global default when the route has no specific configuration."""
        if route is None:
            return self.limit, self.window

        config = self._route_limits.get(route)

        if config is None:
            return self.limit, self.window

        return config["limit"], config["window"]

    @staticmethod
    def _internal_key(key: str, route):
        if route is None:
            return key

        return f"{route}:{key}"

    def _get_limiter(self, key: str, route=None):
        limit, window = self._route_config(route)
        internal_key = self._internal_key(key, route)

        now = time.monotonic()

        with self._lock:
            entry = self._limiters.pop(internal_key, None)

            if entry is not None and now - entry[1] < self.key_ttl:
                self._limiters[internal_key] = entry
                return entry[0]

            limiter = create_rate_limiter(
                algorithm=self.algorithm,
                limit=limit,
                window=window,
                storage=self._storage,
                client_id=internal_key,
            )

            self._limiters[internal_key] = (limiter, now)

            while len(self._limiters) > self.max_keys:
                self._limiters.popitem(last=False)

            return limiter

    def allow_request(self, key: str = "default", route=None) -> bool:
        return self._get_limiter(key, route).allow_request()

    def remaining_requests(self, key: str = "default", route=None) -> int:
        return self._get_limiter(key, route).remaining_requests()

    def reset_time(self, key: str = "default", route=None) -> int:
        return self._get_limiter(key, route).reset_time()

    def rate_limit_headers(self, key: str = "default", route=None) -> dict:
        """Return standard X-RateLimit-* response headers for a client key.

        The limit reflects the route's configured limit (or the global
        default when the route has no specific configuration).
        """
        limit, _ = self._route_config(route)

        return {
            "X-RateLimit-Limit": str(limit),
            "X-RateLimit-Remaining": str(
                max(0, self.remaining_requests(key, route))
            ),
            "X-RateLimit-Reset": str(self.reset_time(key, route)),
        }