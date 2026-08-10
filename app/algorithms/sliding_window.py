import time
from collections import deque


class SlidingWindowRateLimiter:
    def __init__(self, limit: int, window: int):
        self.limit = limit
        self.window = window

        # Stores timestamps of requests
        self.requests = deque()

    def _remove_expired_requests(self):
        current_time = time.time()
        cutoff_time = current_time - self.window

        while self.requests and self.requests[0] <= cutoff_time:
            self.requests.popleft()

    def allow_request(self):
        self._remove_expired_requests()

        if len(self.requests) >= self.limit:
            return False

        self.requests.append(time.time())

        return True

    def remaining_requests(self):
        self._remove_expired_requests()

        return max(0, self.limit - len(self.requests))

    def reset_time(self):
        self._remove_expired_requests()

        if not self.requests:
            return int(time.time())

        return int(self.requests[0] + self.window)