import time


class FixedWindowRateLimiter:
    def __init__(self, limit: int, window: int):
        self.limit = limit
        self.window = window

        self.request_count = 0
        self.window_start = time.time()

    def _reset_if_needed(self):
        current_time = time.time()

        if current_time - self.window_start >= self.window:
            self.request_count = 0
            self.window_start = current_time

    def allow_request(self):
        self._reset_if_needed()

        if self.request_count >= self.limit:
            return False

        self.request_count += 1

        return True

    def remaining_requests(self):
        self._reset_if_needed()

        return max(
            0,
            self.limit - self.request_count
        )

    def reset_time(self):
        self._reset_if_needed()

        return int(
            self.window_start + self.window
        )