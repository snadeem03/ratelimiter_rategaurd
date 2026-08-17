from typing import Optional

import redis

from app.algorithms.base import RateLimiter


class RedisTokenBucketRateLimiter(RateLimiter):

    LUA_SCRIPT = """
    local key = KEYS[1]

    local capacity = tonumber(ARGV[1])
    local refill_rate = tonumber(ARGV[2])
    local ttl_ms = tonumber(ARGV[3])

    local time = redis.call("TIME")
    local now = tonumber(time[1]) + tonumber(time[2]) / 1000000

    local data = redis.call("HMGET", key, "tokens", "last_refill")

    local tokens = tonumber(data[1])
    local last_refill = tonumber(data[2])

    if tokens == nil then
        tokens = capacity
        last_refill = now
    end

    local elapsed = now - last_refill

    if elapsed > 0 then
        tokens = math.min(
            capacity,
            tokens + (elapsed * refill_rate)
        )

        last_refill = now
    end

    local allowed = 0

    if tokens >= 1 then
        tokens = tokens - 1
        allowed = 1
    end

    redis.call(
        "HSET",
        key,
        "tokens",
        tokens,
        "last_refill",
        last_refill
    )

    redis.call("PEXPIRE", key, ttl_ms)

    local remaining = math.floor(tokens)
    local reset_in = 0

    if tokens < 1 and refill_rate > 0 then
        reset_in = math.ceil((1 - tokens) / refill_rate)
    end

    return {
        allowed,
        remaining,
        reset_in
    }
    """

    def __init__(
        self,
        storage,
        client_id: str,
        capacity: int,
        refill_rate: float,
        ttl: Optional[int] = None
    ):
        self.storage = storage
        self.client_id = client_id
        self.capacity = capacity
        self.refill_rate = refill_rate

        if ttl is None:
            ttl = int(capacity / refill_rate) if refill_rate > 0 else 3600
        self.ttl = max(1, ttl)

        self.key = (
            f"rateguard:token_bucket:"
            f"{client_id}"
        )

        self.redis_client = storage.client

        self.script = self.redis_client.register_script(
            self.LUA_SCRIPT
        )

        self._cached_remaining: Optional[int] = None
        self._cached_reset: Optional[int] = None

    def allow_request(self) -> bool:

        result = self.script(
            keys=[self.key],
            args=[
                self.capacity,
                self.refill_rate,
                self.ttl * 1000
            ]
        )

        self._cached_remaining = int(result[1])
        self._cached_reset = int(result[2])

        return bool(result[0])

    def remaining_requests(self) -> int:

        if self._cached_remaining is not None:
            return self._cached_remaining

        data = self.redis_client.hmget(
            self.key,
            "tokens"
        )

        if not data or data[0] is None:
            return self.capacity

        return int(float(data[0]))

    def reset_time(self) -> int:

        if self._cached_reset is not None:
            return self._cached_reset

        tokens = float(
            self.redis_client.hget(
                self.key,
                "tokens"
            ) or self.capacity
        )

        if tokens >= 1:
            return 0

        if self.refill_rate <= 0:
            return 0

        return max(
            0,
            int((1 - tokens) / self.refill_rate)
        )