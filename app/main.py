from fastapi import FastAPI, HTTPException
from app.middleware.rate_limiter import RateLimiter
from app.algorithms.sliding_window import SlidingWindowRateLimiter
from app.algorithms.token_bucket import TokenBucketRateLimiter
from app.algorithms.leaky_bucket import LeakyBucketRateLimiter

app = FastAPI(
    title="RateGuard",
    description="API Rate Limiting Gateway",
    version="1.0.0"
)

# Fixed Window Rate Limiter
# rate_limiter = RateLimiter(
#     limit=5,
#     window=60
# )

# Sliding Window Rate Limiter
# rate_limiter = SlidingWindowRateLimiter(
#     limit=5,
#     window=60
# )

# Token Bucket Rate Limiter
# rate_limiter = TokenBucketRateLimiter(
#     capacity=10,
#     refill_rate=2
# )

# Leaky Bucket Rate Limiter
rate_limiter = LeakyBucketRateLimiter(
    capacity=10,
    leak_rate=2
)

@app.get("/")
def root():
    return {
        "message": "RateGuard is running",
        "version": "1.0.0"
    }


@app.get("/api/test")
def test_api():

    if not rate_limiter.allow_request():
        raise HTTPException(
            status_code=429,
            detail={
                "error": "Too many requests",
                "retry_after": rate_limiter.reset_time()
            }
        )

    return {
        "message": "Request successful",
        "remaining": rate_limiter.remaining_requests()
    }