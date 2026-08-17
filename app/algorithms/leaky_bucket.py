import threading
import time
from collections import deque


class LeakyBucketRateLimiter:
    def __init__(self, capacity: int, leak_rate: float):
        self.capacity = capacity
        self.leak_rate = leak_rate

        self.queue = deque()
        self.last_leak_time = time.monotonic()

        self._lock = threading.Lock()

    def _leak(self):
        current_time = time.monotonic()

        elapsed_time = current_time - self.last_leak_time

        leaked_requests = int(elapsed_time * self.leak_rate)

        if leaked_requests > 0:
            for _ in range(min(leaked_requests, len(self.queue))):
                self.queue.popleft()

            self.last_leak_time = current_time

    def allow_request(self) -> bool:
        with self._lock:
            self._leak()

            if len(self.queue) >= self.capacity:
                return False

            self.queue.append(time.monotonic())

            return True

    def remaining_requests(self) -> int:
        with self._lock:
            self._leak()

            return max(0, self.capacity - len(self.queue))

    def reset_time(self) -> int:
        with self._lock:
            self._leak()

            if not self.queue:
                return 0

            requests_until_space = len(self.queue) - self.capacity + 1

            if requests_until_space <= 0:
                return 0

            if self.leak_rate <= 0:
                return 0

            seconds_until_space = requests_until_space / self.leak_rate

            return max(0, int(seconds_until_space))
