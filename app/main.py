import os

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException

from app.algorithms.factory import create_rate_limiter


load_dotenv()


app = FastAPI(
    title="RateGuard",
    description="API Rate Limiting Gateway",
    version="1.1.0"
)


algorithm = os.getenv(
    "RATE_LIMIT_ALGORITHM",
    "fixed_window"
)

limit = int(
    os.getenv(
        "RATE_LIMIT",
        "5"
    )
)

window = int(
    os.getenv(
        "RATE_LIMIT_WINDOW",
        "60"
    )
)


rate_limiter = create_rate_limiter(
    algorithm=algorithm,
    limit=limit,
    window=window
)


@app.get("/")
def root():
    return {
        "message": "RateGuard is running",
        "version": "1.1.0",
        "algorithm": algorithm
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