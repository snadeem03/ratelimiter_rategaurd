from app.algorithms.fixed_window import FixedWindowRateLimiter
from app.algorithms.sliding_window import SlidingWindowRateLimiter
from app.algorithms.token_bucket import TokenBucketRateLimiter
from app.algorithms.leaky_bucket import LeakyBucketRateLimiter
from app.algorithms.redis_fixed_window import RedisFixedWindowRateLimiter
from app.algorithms.redis_sliding_window import RedisSlidingWindowRateLimiter
from app.algorithms.redis_token_bucket import RedisTokenBucketRateLimiter

def create_rate_limiter(
    algorithm: str,
    limit: int =5,
    window: int =60,
    storage=None,
    client_id: str = "default"
):

    algorithm = algorithm.lower()

    if storage is not None:
        if algorithm == "fixed_window":
            return RedisFixedWindowRateLimiter(
                storage=storage,
                client_id=client_id,
                limit=limit,
                window=window
            )

        if algorithm == "sliding_window":
            return RedisSlidingWindowRateLimiter(
                storage=storage,
                client_id=client_id,
                limit=limit,
                window=window
            )

        if algorithm == "token_bucket":
            return RedisTokenBucketRateLimiter(
                storage=storage,
                client_id=client_id,
                capacity=limit,
                refill_rate=limit/window
            )

        raise ValueError(
            f"Unsupported Redis rate limiter algorithm: {algorithm}"
        )

    if algorithm == "fixed_window":
        return FixedWindowRateLimiter(
            limit=limit,
            window=window
        )

    if algorithm == "sliding_window":
        return SlidingWindowRateLimiter(
            limit=limit,
            window=window
        )

    if algorithm == "token_bucket":
        return TokenBucketRateLimiter(
            capacity=limit,
            refill_rate=limit/window
        )

    if algorithm == "leaky_bucket":
        return LeakyBucketRateLimiter(
            capacity=limit,
            leak_rate=limit/window
        )

    raise ValueError(
        f"Unsupported rate limiter algorithm: {algorithm}"
    )