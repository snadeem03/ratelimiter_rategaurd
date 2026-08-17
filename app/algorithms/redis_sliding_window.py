from typing import Optional

from app.algorithms.base import RateLimiter
from app.storage.keys import sliding_window_key


class RedisSlidingWindowRateLimiter(RateLimiter):

    ALLOW_SCRIPT = """
    local key = KEYS[1]
    local window = tonumber(ARGV[1])
    local limit = tonumber(ARGV[2])

    local time = redis.call("TIME")
    local now = tonumber(time[1]) + tonumber(time[2]) / 1000000

    redis.call("ZREMRANGEBYSCORE", key, 0, now - window)

    local count = redis.call("ZCARD", key)

    local allowed = 0

    if count < limit then
        local seq = redis.call("INCR", key .. ":seq")
        redis.call("ZADD", key, now, tostring(now) .. ":" .. tostring(seq))
        allowed = 1
    end

    local ttl_ms = window * 1000 + 5000
    redis.call("PEXPIRE", key, ttl_ms)
    redis.call("PEXPIRE", key .. ":seq", ttl_ms)

    local remaining = limit - count - allowed
    if remaining < 0 then
        remaining = 0
    end

    local reset_in = 0
    if count >= limit then
        local oldest = redis.call("ZRANGE", key, 0, 0, "WITHSCORES")
        if oldest[2] then
            reset_in = math.ceil((oldest[2] + window) - now)
            if reset_in < 1 then
                reset_in = 1
            end
        end
    end

    return { allowed, remaining, reset_in }
    """

    READ_SCRIPT = """
    local key = KEYS[1]
    local window = tonumber(ARGV[1])
    local limit = tonumber(ARGV[2])

    local time = redis.call("TIME")
    local now = tonumber(time[1]) + tonumber(time[2]) / 1000000

    redis.call("ZREMRANGEBYSCORE", key, 0, now - window)

    local count = redis.call("ZCARD", key)

    local remaining = limit - count
    if remaining < 0 then
        remaining = 0
    end

    local reset_in = 0
    if count >= limit then
        local oldest = redis.call("ZRANGE", key, 0, 0, "WITHSCORES")
        if oldest[2] then
            reset_in = math.ceil((oldest[2] + window) - now)
            if reset_in < 1 then
                reset_in = 1
            end
        end
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

        self.key = sliding_window_key(client_id)

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
            args=[self.window, self.limit]
        )

        self._cached_remaining = int(result[1])
        self._cached_reset = int(result[2])

        return bool(result[0])

    def remaining_requests(self) -> int:

        if self._cached_remaining is not None:
            return self._cached_remaining

        result = self._read_script(
            keys=[self.key],
            args=[self.window, self.limit]
        )

        return int(result[0])

    def reset_time(self) -> int:

        if self._cached_reset is not None:
            return self._cached_reset

        result = self._read_script(
            keys=[self.key],
            args=[self.window, self.limit]
        )

        return int(result[1])