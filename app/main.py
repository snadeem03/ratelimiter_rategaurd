import hmac
import os
from pathlib import Path

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from app.api_keys import ApiKeyStore, RedisApiKeyStore
from app.config import parse_route_limits
from app.metrics import metrics_body
from app.middleware.rate_limiter import RateLimiter
from app.middleware.rate_limit_middleware import RateLimitMiddleware
from app.playground import simulation as playground_sim


load_dotenv()


app = FastAPI(
    title="RateGuard",
    description="API Rate Limiting Gateway",
    version="1.1.0"
)


def _env_int(name: str, default: str) -> int:
    """Read an integer environment variable, failing fast with a clear
    error when the value is not a valid integer."""
    raw = os.getenv(name, default)

    try:
        return int(raw)
    except ValueError:
        raise RuntimeError(
            f"{name} must be an integer, got {raw!r}"
        ) from None


algorithm = os.getenv(
    "RATE_LIMIT_ALGORITHM",
    "sliding_window"
)

limit = _env_int("RATE_LIMIT", "5")
window = _env_int("RATE_LIMIT_WINDOW", "60")

if limit < 1:
    raise RuntimeError(f"RATE_LIMIT must be >= 1, got {limit}")

if window < 1:
    raise RuntimeError(f"RATE_LIMIT_WINDOW must be >= 1, got {window}")

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

EXCLUDED_PATHS = {"/", "/docs", "/redoc", "/openapi.json", "/metrics"}

# Route label values used by the Prometheus metrics. Configured per-route
# limits are known routes too; everything else is aggregated under "other".
METRICS_KNOWN_ROUTES = frozenset(
    {
        *EXCLUDED_PATHS,
        *route_limits,
        "/api/test",
        "/api/login",
        "/api/products",
        "/api/orders",
    }
)

API_KEY_PREFIX = os.getenv(
    "API_KEY_PREFIX",
    "rg_live_"
)

API_KEY_STORE_PATH = os.getenv(
    "API_KEY_STORE_PATH",
    "api_keys.json"
)

PLAYGROUND_STATIC_DIR = Path(__file__).resolve().parent / "static"

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


@app.get("/metrics")
def prometheus_metrics():
    """Prometheus exposition endpoint (never rate-limited).

    Counters are process-local unless PROMETHEUS_MULTIPROC_DIR is set,
    in which case the workers of this container are aggregated at scrape
    time by prometheus_client's multiprocess collector.
    """
    return Response(
        content=metrics_body(),
        media_type="text/plain; version=0.0.4; charset=utf-8",
    )


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

    # Compare bytes: ``hmac.compare_digest`` raises TypeError for
    # non-ASCII ``str`` inputs, which would turn a malformed token into
    # a 500 instead of a 403.
    if (
        not supplied
        or not hmac.compare_digest(
            supplied.encode("utf-8"),
            ADMIN_API_TOKEN.encode("utf-8"),
        )
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


class PlaygroundSimCreate(BaseModel):
    algorithm: str
    limit: int = 10
    window: int = 60
    backend: str = "memory"
    client_id: str = "client-1"
    route: str = "/api/test"


class PlaygroundSimRequest(BaseModel):
    session_id: str
    count: int = 1


class PlaygroundSession(BaseModel):
    session_id: str


def _playground_sim_payload(session):
    payload = playground_sim.session_payload(session)
    payload["session_id"] = session.session_id
    return payload


@app.get("/playground/api/config")
def playground_api_config():
    """Report the running server's real configuration and Redis
    reachability. Never exposes credentials or tokens."""
    redis_ok = False

    try:
        from app.core.redis_client import get_redis
        redis_ok = bool(get_redis().ping())
    except Exception:
        redis_ok = False

    return {
        "version": "1.1.0",
        "algorithm": algorithm,
        "backend": RATE_LIMIT_BACKEND,
        "limit": limit,
        "window": window,
        "route_limits": route_limits,
        "api_key_prefix": API_KEY_PREFIX,
        "trust_proxy_headers": TRUST_PROXY_HEADERS,
        "redis": {
            "configured": RATE_LIMIT_BACKEND == "redis",
            "available": redis_ok,
        },
    }


@app.post("/playground/sim/session", status_code=201)
def playground_sim_create(body: PlaygroundSimCreate):
    """Create a simulation session backed by a real RateGuard limiter."""
    try:
        session = playground_sim.create_session(
            algorithm=body.algorithm,
            limit=body.limit,
            window=body.window,
            backend=body.backend,
            client_id=body.client_id,
            route=body.route,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except playground_sim.RedisUnavailable as exc:
        raise HTTPException(
            status_code=503,
            detail={"error": "Redis unavailable"},
        ) from exc

    return _playground_sim_payload(session)


@app.post("/playground/sim/request")
def playground_sim_request(body: PlaygroundSimRequest):
    """Send ``count`` requests through the session's real limiter."""
    session = playground_sim.get_session(body.session_id)

    if session is None:
        raise HTTPException(
            status_code=404,
            detail="Simulation session not found",
        )

    if body.count < 1 or body.count > playground_sim.MAX_BURST:
        raise HTTPException(
            status_code=422,
            detail=(
                f"count must be between 1 and "
                f"{playground_sim.MAX_BURST}"
            ),
        )

    try:
        return session.send(body.count)
    except playground_sim.RedisUnavailable as exc:
        raise HTTPException(
            status_code=503,
            detail={"error": "Redis unavailable"},
        ) from exc


@app.get("/playground/sim/state")
def playground_sim_state(session_id: str):
    """Read the session's current state without consuming a request."""
    session = playground_sim.get_session(session_id)

    if session is None:
        raise HTTPException(
            status_code=404,
            detail="Simulation session not found",
        )

    try:
        return _playground_sim_payload(session)
    except playground_sim.RedisUnavailable as exc:
        raise HTTPException(
            status_code=503,
            detail={"error": "Redis unavailable"},
        ) from exc


@app.post("/playground/sim/reset")
def playground_sim_reset(body: PlaygroundSession):
    """Recreate the session's limiter and clear recorded metrics/events."""
    session = playground_sim.get_session(body.session_id)

    if session is None:
        raise HTTPException(
            status_code=404,
            detail="Simulation session not found",
        )

    try:
        session.reset()
        return _playground_sim_payload(session)
    except playground_sim.RedisUnavailable as exc:
        raise HTTPException(
            status_code=503,
            detail={"error": "Redis unavailable"},
        ) from exc


@app.post("/playground/sim/close")
def playground_sim_close(body: PlaygroundSession):
    """Release a simulation session (idempotent)."""
    playground_sim.close_session(body.session_id)
    return {"closed": True}


app.mount(
    "/playground",
    StaticFiles(directory=str(PLAYGROUND_STATIC_DIR), html=True),
    name="playground",
)

app.add_middleware(
    RateLimitMiddleware,
    client_key_fn=client_key,
    get_rate_limiter=lambda: rate_limiter,
    excluded_paths=EXCLUDED_PATHS,
    route_limits=route_limits,
    excluded_prefixes=("/admin", "/playground"),
    known_routes=METRICS_KNOWN_ROUTES,
)