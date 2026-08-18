from typing import Optional

from app.algorithms.base import RateLimiter
from app.storage.keys import leaky_bucket_key


class RedisLeakyBucketRateLimiter(RateLimiter):

    ALLOW_SCRIPT = """
    local key = KEYS[1]
    local seq_key = KEYS[2]
    local last_leak_key = KEYS[3]

    local capacity = tonumber(ARGV[1])
    local leak_rate = tonumber(ARGV[2])
    local ttl_ms = tonumber(ARGV[3])

    local time = redis.call("TIME")
    local now = tonumber(time[1]) + tonumber(time[2]) / 1000000

    local last = redis.call("GET", last_leak_key)
    if last == false then
        redis.call("SET", last_leak_key, now)
        last = now
    else
        last = tonumber(last)
    end

    local leaked = 0
    if now > last then
        leaked = math.floor((now - last) * leak_rate)
        if leaked > 0 then
            redis.call("ZREMRANGEBYRANK", key, 0, leaked - 1)
            redis.call("SET", last_leak_key, now)
        end
    end

    local count = redis.call("ZCARD", key)

    local allowed = 0
    if count < capacity then
        local seq = redis.call("INCR", seq_key)
        redis.call("ZADD", key, now, tostring(now) .. ":" .. tostring(seq))
        allowed = 1
    end

    redis.call("PEXPIRE", key, ttl_ms)
    redis.call("PEXPIRE", seq_key, ttl_ms)
    redis.call("PEXPIRE", last_leak_key, ttl_ms)

    local remaining = capacity - count - allowed
    if remaining < 0 then
        remaining = 0
    end

    local reset_in = 0
    if count >= capacity then
        if leak_rate > 0 then
            reset_in = math.ceil(1 / leak_rate)
            if reset_in < 1 then
                reset_in = 1
            end
        end
    end

    return { allowed, remaining, reset_in }
    """

    READ_SCRIPT = """
    local key = KEYS[1]
    local last_leak_key = KEYS[2]

    local capacity = tonumber(ARGV[1])
    local leak_rate = tonumber(ARGV[2])
    local ttl_ms = tonumber(ARGV[3])

    local time = redis.call("TIME")
    local now = tonumber(time[1]) + tonumber(time[2]) / 1000000

    local last = redis.call("GET", last_leak_key)
    if last == false then
        redis.call("SET", last_leak_key, now)
        redis.call("PEXPIRE", last_leak_key, ttl_ms)
        last = now
    else
        last = tonumber(last)
    end

    local leaked = 0
    if now > last then
        leaked = math.floor((now - last) * leak_rate)
        if leaked > 0 then
            redis.call("ZREMRANGEBYRANK", key, 0, leaked - 1)
            redis.call("SET", last_leak_key, now)
            redis.call("PEXPIRE", last_leak_key, ttl_ms)
        end
    end

    local count = redis.call("ZCARD", key)

    local remaining = capacity - count
    if remaining < 0 then
        remaining = 0
    end

    local reset_in = 0
    if count >= capacity then
        if leak_rate > 0 then
            reset_in = math.ceil(1 / leak_rate)
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
        capacity: int,
        leak_rate: float,
        ttl: Optional[int] = None
    ):
        self.storage = storage
        self.client_id = client_id
        self.capacity = capacity
        self.leak_rate = leak_rate

        if ttl is None:
            ttl = int(capacity / leak_rate) if leak_rate > 0 else 3600
        self.ttl = max(1, ttl)

        self.key = leaky_bucket_key(client_id)
        self.seq_key = f"{self.key}:seq"
        self.last_leak_key = f"{self.key}:last_leak"

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
            keys=[self.key, self.seq_key, self.last_leak_key],
            args=[self.capacity, self.leak_rate, self.ttl * 1000]
        )

        self._cached_remaining = int(result[1])
        self._cached_reset = int(result[2])

        return bool(result[0])

    def remaining_requests(self) -> int:

        if self._cached_remaining is not None:
            return self._cached_remaining

        result = self._read_script(
            keys=[self.key, self.last_leak_key],
            args=[self.capacity, self.leak_rate, self.ttl * 1000]
        )

        return int(result[0])

    def reset_time(self) -> int:

        if self._cached_reset is not None:
            return self._cached_reset

        result = self._read_script(
            keys=[self.key, self.last_leak_key],
            args=[self.capacity, self.leak_rate, self.ttl * 1000]
        )

        return int(result[1])
