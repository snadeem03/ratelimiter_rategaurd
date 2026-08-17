import threading
import time
from collections import OrderedDict

from app.algorithms.factory import create_rate_limiter


class RateLimiter:
    """
    Thread-safe, per-client rate limiter facade.

    Holds one algorithm instance per client key, so each client is
    limited independently. Idle keys are evicted (LRU + TTL) so memory
    stays bounded even with a very large number of distinct clients.
    """

    def __init__(
        self,
        limit: int = 5,
        window: int = 60,
        algorithm: str = "sliding_window",
        max_keys: int = 10_000,
        key_ttl: float = 3_600,
    ):
        self.limit = limit
        self.window = window
        self.algorithm = algorithm
        self.max_keys = max_keys
        self.key_ttl = key_ttl

        # key -> (limiter, last_seen_monotonic)
        self._limiters = OrderedDict()
        self._lock = threading.Lock()

    def _get_limiter(self, key: str):
        now = time.monotonic()

        with self._lock:
            entry = self._limiters.pop(key, None)

            if entry is not None and now - entry[1] < self.key_ttl:
                self._limiters[key] = entry
                return entry[0]

            limiter = create_rate_limiter(
                algorithm=self.algorithm,
                limit=self.limit,
                window=self.window,
            )

            self._limiters[key] = (limiter, now)

            while len(self._limiters) > self.max_keys:
                self._limiters.popitem(last=False)

            return limiter

    def allow_request(self, key: str = "default") -> bool:
        return self._get_limiter(key).allow_request()

    def remaining_requests(self, key: str = "default") -> int:
        return self._get_limiter(key).remaining_requests()

    def reset_time(self, key: str = "default") -> int:
        return self._get_limiter(key).reset_time()