import hmac
import os

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from app.api_keys import ApiKeyStore, RedisApiKeyStore
from app.config import parse_route_limits
from app.middleware.rate_limiter import RateLimiter
from app.middleware.rate_limit_middleware import RateLimitMiddleware


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

EXCLUDED_PATHS = {"/", "/docs", "/redoc", "/openapi.json"}

API_KEY_PREFIX = os.getenv(
    "API_KEY_PREFIX",
    "rg_live_"
)

API_KEY_STORE_PATH = os.getenv(
    "API_KEY_STORE_PATH",
    "api_keys.json"
)

ADMIN_API_TOKEN = os.getenv("ADMIN_API_TOKEN")

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

    api_key_store = RedisApiKeyStore(
        redis,
        prefix=API_KEY_PREFIX
    )

else:
    api_key_store = ApiKeyStore(
        path=API_KEY_STORE_PATH,
        prefix=API_KEY_PREFIX
    )


rate_limiter = RateLimiter(
    algorithm=algorithm,
    limit=limit,
    window=window,
    storage=storage,
    route_limits=route_limits
)


def client_key(request: Request) -> str:
    """Resolve the rate-limit identity for a request.

    A managed API key (recognised by ``API_KEY_PREFIX``) becomes the
    primary identity and is authenticated against the key store:
    missing/disabled/expired keys raise 401. Header values that are not
    managed keys keep the legacy opaque-client behaviour, and requests
    without an API key fall back to the client IP.
    """
    api_key = request.headers.get("X-API-Key")

    if api_key:
        if api_key.startswith(API_KEY_PREFIX):
            record = api_key_store.authenticate(api_key)

            if record is None:
                raise HTTPException(
                    status_code=401,
                    detail={
                        "error": "Invalid or inactive API key"
                    }
                )

            identity = record.get("owner") or record["hash"]

            return f"apikey:{identity}"

        return f"apikey:{api_key}"

    if TRUST_PROXY_HEADERS:
        forwarded = request.headers.get("X-Forwarded-For")
        if forwarded:
            return f"ip:{forwarded.split(',')[0].strip()}"

    host = request.client.host if request.client else "unknown"
    return f"ip:{host}"


def _remaining(request: Request) -> int:
    """Return the remaining allowance recorded by the middleware."""
    return int(
        request.state.rate_limit_headers["X-RateLimit-Remaining"]
    )


@app.get("/")
def root():
    return {
        "message": "RateGuard is running",
        "version": "1.1.0",
        "algorithm": algorithm
    }


@app.get("/api/test")
def test_api(request: Request):
    return JSONResponse(
        content={
            "message": "Request successful",
            "remaining": _remaining(request)
        }
    )


@app.post("/api/login")
def login(request: Request):
    return JSONResponse(
        content={
            "message": "Login successful",
            "remaining": _remaining(request)
        }
    )


@app.get("/api/products")
def products(request: Request):
    return JSONResponse(
        content={
            "message": "Products fetched",
            "remaining": _remaining(request)
        }
    )


@app.post("/api/orders")
def orders(request: Request):
    return JSONResponse(
        content={
            "message": "Order created",
            "remaining": _remaining(request)
        }
    )


class CreateApiKeyRequest(BaseModel):
    name: str
    owner: str | None = None
    ttl: int | None = None


def admin_required(request: Request):
    """Reject admin requests unless an admin token is configured and supplied.

    The token is read from the ``ADMIN_API_TOKEN`` environment variable and
    compared in constant time. When no token is configured, the admin API is
    disabled entirely (nothing is publicly accessible by default).
    """
    if not ADMIN_API_TOKEN:
        raise HTTPException(
            status_code=403,
            detail="Admin API is not configured"
        )

    supplied = request.headers.get("X-Admin-Token")

    if not supplied or not hmac.compare_digest(
        supplied,
        ADMIN_API_TOKEN
    ):
        raise HTTPException(
            status_code=403,
            detail="Invalid admin token"
        )


@app.post("/admin/api-keys", status_code=201, dependencies=[Depends(admin_required)])
def create_api_key(body: CreateApiKeyRequest):
    """Create an API key. The plaintext key is returned only here."""
    key, metadata = api_key_store.create(
        name=body.name,
        owner=body.owner,
        ttl=body.ttl
    )

    return {
        "key": key,
        **metadata
    }


@app.get("/admin/api-keys", dependencies=[Depends(admin_required)])
def list_api_keys():
    """List key metadata. Secrets are never included."""
    return {
        "api_keys": api_key_store.list()
    }


@app.post(
    "/admin/api-keys/{key_id}/revoke",
    dependencies=[Depends(admin_required)]
)
def revoke_api_key(key_id: str):
    """Disable an API key so it can no longer authenticate."""
    if not api_key_store.revoke(key_id):
        raise HTTPException(
            status_code=404,
            detail="API key not found"
        )

    return {
        "revoked": key_id
    }


@app.delete(
    "/admin/api-keys/{key_id}",
    status_code=204,
    dependencies=[Depends(admin_required)]
)
def delete_api_key(key_id: str):
    """Permanently remove an API key."""
    if not api_key_store.delete(key_id):
        raise HTTPException(
            status_code=404,
            detail="API key not found"
        )


app.add_middleware(
    RateLimitMiddleware,
    client_key_fn=client_key,
    get_rate_limiter=lambda: rate_limiter,
    excluded_paths=EXCLUDED_PATHS,
    route_limits=route_limits,
)