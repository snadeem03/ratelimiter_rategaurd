from app.algorithms.fixed_window import FixedWindowRateLimiter
from app.algorithms.sliding_window import SlidingWindowRateLimiter
from app.algorithms.token_bucket import TokenBucketRateLimiter
from app.algorithms.leaky_bucket import LeakyBucketRateLimiter

def create_rate_limiter(
    algorithm: str,
    limit: int =5,
    window: int =60
):

    algorithm = algorithm.lower()

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

