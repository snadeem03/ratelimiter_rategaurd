from fastapi import FastAPI, HTTPException
from app.middleware.rate_limiter import RateLimiter
from app.algorithms.sliding_window import SlidingWindowRateLimiter


app = FastAPI(
    title="RateGuard",
    description="API Rate Limiting Gateway",
    version="1.0.0"
)


# rate_limiter = RateLimiter(
#     limit=5,
#     window=60
# )

rate_limiter = SlidingWindowRateLimiter(
    limit=5,
    window=60
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