import os

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from app.config import parse_route_limits
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

route_limits = parse_route_limits(
    os.getenv("RATE_LIMIT_ROUTES", "")
)

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
    storage=storage,
    route_limits=route_limits
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


def _enforce_rate_limit(request: Request, route: str) -> dict:
    """Check the rate limit for a client on a route and return headers.

    Raises HTTPException(429) with Retry-After when over the limit.
    """
    key = client_key(request)

    allowed = rate_limiter.allow_request(key, route=route)

    headers = rate_limiter.rate_limit_headers(key, route=route)

    if not allowed:
        retry_after = headers["X-RateLimit-Reset"]

        raise HTTPException(
            status_code=429,
            detail={
                "error": "Too many requests",
                "retry_after": int(retry_after)
            },
            headers={
                **headers,
                "Retry-After": retry_after
            }
        )

    return headers


@app.get("/")
def root():
    return {
        "message": "RateGuard is running",
        "version": "1.1.0",
        "algorithm": algorithm
    }


@app.get("/api/test")
def test_api(request: Request):
    headers = _enforce_rate_limit(request, route="/api/test")

    return JSONResponse(
        content={
            "message": "Request successful",
            "remaining": int(headers["X-RateLimit-Remaining"])
        },
        headers=headers
    )


@app.post("/api/login")
def login(request: Request):
    headers = _enforce_rate_limit(request, route="/api/login")

    return JSONResponse(
        content={
            "message": "Login successful",
            "remaining": int(headers["X-RateLimit-Remaining"])
        },
        headers=headers
    )


@app.get("/api/products")
def products(request: Request):
    headers = _enforce_rate_limit(request, route="/api/products")

    return JSONResponse(
        content={
            "message": "Products fetched",
            "remaining": int(headers["X-RateLimit-Remaining"])
        },
        headers=headers
    )


@app.post("/api/orders")
def orders(request: Request):
    headers = _enforce_rate_limit(request, route="/api/orders")

    return JSONResponse(
        content={
            "message": "Order created",
            "remaining": int(headers["X-RateLimit-Remaining"])
        },
        headers=headers
    )