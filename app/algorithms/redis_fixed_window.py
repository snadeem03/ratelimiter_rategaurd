from typing import Optional

from app.algorithms.base import RateLimiter
from app.storage.keys import fixed_window_key


class RedisFixedWindowRateLimiter(RateLimiter):

    ALLOW_SCRIPT = """
    local current = redis.call("INCR", KEYS[1])

    if current == 1 then
        redis.call("EXPIRE", KEYS[1], ARGV[2])
    end

    local limit = tonumber(ARGV[1])

    local allowed = 0
    if current <= limit then
        allowed = 1
    end

    local remaining = limit - current
    if remaining < 0 then
        remaining = 0
    end

    local pttl = redis.call("PTTL", KEYS[1])
    local reset_in = 0
    if pttl > 0 then
        reset_in = math.ceil(pttl / 1000)
    end

    return { allowed, remaining, reset_in }
    """

    READ_SCRIPT = """
    local current = tonumber(redis.call("GET", KEYS[1]) or "0")
    local limit = tonumber(ARGV[1])

    local remaining = limit - current
    if remaining < 0 then
        remaining = 0
    end

    local pttl = redis.call("PTTL", KEYS[1])
    local reset_in = 0
    if pttl > 0 then
        reset_in = math.ceil(pttl / 1000)
    end

    return { remaining, reset_in }
    """

    def __init__(
        self,
        storage,
        client_id: str,
        limit: int,
        window: int
    ):
        self.storage = storage
        self.client_id = client_id
        self.limit = limit
        self.window = window

        self.key = fixed_window_key(client_id)

        self.redis_client = storage.client

        self._allow_script = self.redis_client.register_script(
            self.ALLOW_SCRIPT
        )

        self._read_script = self.redis_client.register_script(
            self.READ_SCRIPT
        )

        self._cached_remaining: Optional[int] = None
        self._cached_reset: Optional[int] = None

    def allow_request(self) -> bool:

        result = self._allow_script(
            keys=[self.key],
            args=[self.limit, self.window]
        )

        self._cached_remaining = int(result[1])
        self._cached_reset = int(result[2])

        return bool(result[0])

    def remaining_requests(self) -> int:

        if self._cached_remaining is not None:
            return self._cached_remaining

        result = self._read_script(
            keys=[self.key],
            args=[self.limit]
        )

        return int(result[0])

    def reset_time(self) -> int:

        if self._cached_reset is not None:
            return self._cached_reset

        result = self._read_script(
            keys=[self.key],
            args=[self.limit]
        )

        return int(result[1])