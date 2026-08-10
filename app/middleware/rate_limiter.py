import time
from collections import deque
from typing import Dict


class RateLimiter:
    """
    In-memory rate limiter using a sliding window algorithm.
    """

    def __init__(self, limit: int = 5, window: int = 60):
        self.limit = limit
        self.window = window  # Window duration in seconds
        self._requests: Dict[str, deque] = {}

    def _clean_old_requests(self, key: str, current_time: float) -> None:
        """Remove timestamps that fall outside the sliding window."""
        if key in self._requests:
            cutoff = current_time - self.window
            while self._requests[key] and self._requests[key][0] <= cutoff:
                self._requests[key].popleft()

    def allow_request(self, key: str = "default") -> bool:
        """
        Check if a request is allowed for the given key.
        If allowed, records the request timestamp and returns True.
        Otherwise returns False.
        """
        now = time.time()
        self._clean_old_requests(key, now)

        if key not in self._requests:
            self._requests[key] = deque()

        if len(self._requests[key]) < self.limit:
            self._requests[key].append(now)
            return True
        return False

    def remaining_requests(self, key: str = "default") -> int:
        """
        Return the number of remaining allowed requests within the current window.
        """
        now = time.time()
        self._clean_old_requests(key, now)
        current_count = len(self._requests.get(key, []))
        return max(0, self.limit - current_count)

    def reset_time(self, key: str = "default") -> int:
        """
        Return the time in seconds until the oldest request in the window expires,
        allowing a new request.
        """
        now = time.time()
        self._clean_old_requests(key, now)

        if key in self._requests and self._requests[key]:
            oldest_request = self._requests[key][0]
            time_until_reset = (oldest_request + self.window) - now
            return max(0, int(round(time_until_reset)))
        return 0
