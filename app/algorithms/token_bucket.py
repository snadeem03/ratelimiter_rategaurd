import time


class TokenBucketRateLimiter:
    def __init__(self, capacity: int, refill_rate: float):
        self.capacity = capacity
        self.refill_rate = refill_rate

        # Start with a full bucket
        self.tokens = float(capacity)
        self.last_refill_time = time.time()

    def _refill(self):
        current_time = time.time()

        elapsed_time = current_time - self.last_refill_time

        new_tokens = elapsed_time * self.refill_rate

        self.tokens = min(
            self.capacity,
            self.tokens + new_tokens
        )

        self.last_refill_time = current_time

    def allow_request(self):
        self._refill()

        if self.tokens < 1:
            return False

        self.tokens -= 1

        return True

    def remaining_requests(self):
        self._refill()

        return int(self.tokens)

    def reset_time(self):
        self._refill()

        if self.tokens >= 1:
            return int(time.time())

        seconds_until_token = (1 - self.tokens) / self.refill_rate

        return int(time.time() + seconds_until_token)