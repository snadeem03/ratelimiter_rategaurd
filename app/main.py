import os

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request

from app.middleware.rate_limiter import RateLimiter


load_dotenv()


app = FastAPI(
    title="RateGuard",
    description="API Rate Limiting Gateway",
    version="1.1.0"
)


algorithm = os.getenv(
    "RATE_LIMIT_ALGORITHM",
    "sliding_window"
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

TRUST_PROXY_HEADERS = os.getenv(
    "TRUST_PROXY_HEADERS",
    ""
).lower() in ("1", "true", "yes")

RATE_LIMIT_BACKEND = os.getenv(
    "RATE_LIMIT_BACKEND",
    "memory"
).lower().strip()

storage = None

if RATE_LIMIT_BACKEND == "redis":
    from app.core.redis_client import get_redis
    from app.storage.redis_storage import RedisStorage

    redis = get_redis()

    try:
        redis.ping()
    except Exception as exc:
        raise RuntimeError(
            "RATE_LIMIT_BACKEND=redis but Redis is unreachable. "
            "Check REDIS_URL and that Redis is running."
        ) from exc

    storage = RedisStorage(redis)


rate_limiter = RateLimiter(
    algorithm=algorithm,
    limit=limit,
    window=window,
    storage=storage
)


def client_key(request: Request) -> str:
    api_key = request.headers.get("X-API-Key")
    if api_key:
        return f"apikey:{api_key}"

    if TRUST_PROXY_HEADERS:
        forwarded = request.headers.get("X-Forwarded-For")
        if forwarded:
            return f"ip:{forwarded.split(',')[0].strip()}"

    host = request.client.host if request.client else "unknown"
    return f"ip:{host}"


@app.get("/")
def root():
    return {
        "message": "RateGuard is running",
        "version": "1.1.0",
        "algorithm": algorithm
    }


@app.get("/api/test")
def test_api(request: Request):

    key = client_key(request)

    if not rate_limiter.allow_request(key):
        retry_after = rate_limiter.reset_time(key)

        raise HTTPException(
            status_code=429,
            detail={
                "error": "Too many requests",
                "retry_after": retry_after
            },
            headers={"Retry-After": str(retry_after)}
        )

    return {
        "message": "Request successful",
        "remaining": rate_limiter.remaining_requests(key)
    }