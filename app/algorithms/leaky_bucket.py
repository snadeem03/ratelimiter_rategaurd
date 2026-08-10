import time
from collections import deque


class LeakyBucketRateLimiter:
    def __init__(self, capacity: int, leak_rate: float):
        self.capacity = capacity
        self.leak_rate = leak_rate

        self.queue = deque()
        self.last_leak_time = time.time()

    def _leak(self):
        current_time = time.time()

        elapsed_time = current_time - self.last_leak_time

        leaked_requests = int(elapsed_time * self.leak_rate)

        if leaked_requests > 0:
            for _ in range(min(leaked_requests, len(self.queue))):
                self.queue.popleft()

            self.last_leak_time = current_time

    def allow_request(self):
        self._leak()

        if len(self.queue) >= self.capacity:
            return False

        self.queue.append(time.time())

        return True

    def remaining_requests(self):
        self._leak()

        return max(0, self.capacity - len(self.queue))

    def reset_time(self):
        self._leak()

        if not self.queue:
            return int(time.time())

        requests_until_space = len(self.queue) - self.capacity + 1

        if requests_until_space <= 0:
            return int(time.time())

        seconds_until_space = requests_until_space / self.leak_rate

        return int(time.time() + seconds_until_space)