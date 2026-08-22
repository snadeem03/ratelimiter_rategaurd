import hmac
import os
from pathlib import Path

from dotenv import load_dotenv
from fastapi import (
    Depends,
    FastAPI,
    HTTPException,
    Query,
    Request,
    Response,
)
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from app.api_keys import ApiKeyStore, RedisApiKeyStore
from app.config import parse_route_limits
from app.metrics import (
    metrics_body,
    record_policy_audit_event,
    record_policy_operation,
)
from app.middleware.rate_limiter import RateLimiter
from app.middleware.rate_limit_middleware import RateLimitMiddleware
from app.playground import simulation as playground_sim
from app.policies.audit import (
    AUDIT_OPERATIONS,
    MemoryAuditStore,
    RedisAuditStore,
    new_event as new_audit_event,
)
from app.policies.model import (
    RoutePolicy,
    normalize_policy_payload,
    validate_route,
)
from app.policies.resolver import (
    PolicyResolver,
    SOURCE_DYNAMIC,
    SOURCE_GLOBAL,
    SOURCE_STATIC,
)
from app.policies.store import (
    MemoryPolicyStore,
    PolicyError,
    RedisPolicyStore,
)


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

# Bounded retention for the policy audit trail (newest events kept).
AUDIT_MAX_EVENTS = _env_int("RATE_LIMIT_AUDIT_MAX_EVENTS", "1000")

if AUDIT_MAX_EVENTS < 1:
    raise RuntimeError(
        f"RATE_LIMIT_AUDIT_MAX_EVENTS must be >= 1, got {AUDIT_MAX_EVENTS}"
    )

if AUDIT_MAX_EVENTS > 1_000_000:
    raise RuntimeError(
        "RATE_LIMIT_AUDIT_MAX_EVENTS must be <= 1000000, "
        f"got {AUDIT_MAX_EVENTS}"
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

    # Dynamic policies live in Redis so every worker observes the same
    # runtime configuration. No TTL: they persist until deleted.
    policy_store = RedisPolicyStore(redis)

else:
    api_key_store = ApiKeyStore(
        path=API_KEY_STORE_PATH,
        prefix=API_KEY_PREFIX
    )

    # Process-local by design: memory-mode policies are NOT shared
    # between workers and do not survive restarts.
    policy_store = MemoryPolicyStore()


# Audit trail for dynamic policy changes. Redis mode shares one bounded
# stream across every worker; memory mode is process-local and ephemeral.
if RATE_LIMIT_BACKEND == "redis":
    policy_audit_store = RedisAuditStore(redis, max_events=AUDIT_MAX_EVENTS)
else:
    policy_audit_store = MemoryAuditStore(max_events=AUDIT_MAX_EVENTS)

policy_store.audit = policy_audit_store


policy_resolver = PolicyResolver(
    store=policy_store,
    static_route_limits=route_limits,
    global_limit=limit,
    global_window=window,
)


rate_limiter = RateLimiter(
    algorithm=algorithm,
    limit=limit,
    window=window,
    storage=storage,
    route_limits=route_limits,
    policy_resolver=policy_resolver,
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


# --------------------------------------------------------------- policies
# Runtime-managed rate-limit policies. Routes contain "/" characters, so
# the path parameter uses Starlette's ``:path`` converter, e.g.
# PUT /admin/rate-limits/api/orders  ->  route = "/api/orders".


class PolicyUnavailable(Exception):
    """Raised when the policy store cannot be reached."""


def _policy_route(route: str, from_url: bool = True) -> str:
    """Validate a route; ValueError -> 422.

    Starlette's ``:path`` converter consumes the separator after
    ``/admin/rate-limits``, so URL-captured values arrive without
    their leading ``/`` and it is restored here. Routes taken from a
    request body must already start with ``/``.
    """
    candidate = route

    if from_url and isinstance(route, str) and not route.startswith("/"):
        candidate = f"/{route}"

    try:
        return validate_route(candidate)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


def _policy_entry(route: str) -> dict:
    """Build the effective-config view for one route."""
    try:
        effective = policy_resolver.effective(route)
    except Exception as exc:
        raise PolicyUnavailable() from exc

    entry = {
        "limit": effective.limit,
        "window": effective.window,
        "source": effective.source,
    }

    if effective.policy is not None:
        entry["policy"] = effective.policy.to_dict()

    return entry


def _policies_from_store(operation):
    """Run a read against the policy store, mapping failures."""
    try:
        return policy_resolver.list_policies()
    except Exception as exc:
        record_policy_operation(operation, "error")
        raise PolicyUnavailable() from exc


def _audited_write(write, operation: str):
    """Run a combined policy+audit write, marking audit failures.

    The write is atomic (single Lua execution on Redis, single lock in
    memory mode): either the policy change and its audit event both
    persist, or neither does. Any failure surfaces as 503 via
    ``PolicyUnavailable`` — a successful policy change is never
    reported while its audit event was silently lost.
    """
    try:
        return write()
    except Exception as exc:
        record_policy_audit_event(operation, "error")
        raise PolicyUnavailable() from exc


@app.get("/admin/rate-limits", dependencies=[Depends(admin_required)])
def list_rate_limits():
    """List every route with its effective limit and its source.

    ``routes`` merges static configuration with dynamic policies under
    the documented precedence (dynamic > static > global); ``policies``
    lists the raw runtime-managed policies.
    """
    policies = _policies_from_store("list")

    routes = {
        route: {
            "limit": config["limit"],
            "window": config["window"],
            "source": SOURCE_STATIC,
        }
        for route, config in sorted(route_limits.items())
    }

    for policy in policies:
        routes[policy.route] = _policy_entry(policy.route)

    return {
        "global": {"limit": limit, "window": window},
        "routes": routes,
        "policies": [policy.to_dict() for policy in policies],
    }


@app.get(
    "/admin/rate-limits/audit",
    dependencies=[Depends(admin_required)]
)
def list_policy_audit_history(
    limit: int = Query(50, ge=1, le=500),
    route: str | None = None,
    operation: str | None = None,
):
    """Recent dynamic policy audit events (newest first).

    Optional exact-match filters: ``route`` (must start with "/") and
    ``operation`` (create | update | delete). Reads stay bounded: at
    most a small recent window of the stream is examined per call.
    """
    safe_route = None

    if route is not None:
        try:
            safe_route = validate_route(route)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    if operation is not None and operation not in AUDIT_OPERATIONS:
        allowed = ", ".join(sorted(AUDIT_OPERATIONS))
        raise HTTPException(
            status_code=422,
            detail=f"operation must be one of {allowed}",
        )

    try:
        events = policy_audit_store.list(
            limit=limit,
            route=safe_route,
            operation=operation,
        )
    except Exception as exc:
        raise PolicyUnavailable() from exc

    return {
        "events": [event.to_dict() for event in events],
        "count": len(events),
    }


@app.get(
    "/admin/rate-limits/{route:path}",
    dependencies=[Depends(admin_required)]
)
def get_rate_limit(route: str):
    """Show the effective configuration for one route."""
    safe = _policy_route(route)

    return _policy_entry(safe)


@app.post("/admin/rate-limits", status_code=201,
          dependencies=[Depends(admin_required)])
def create_rate_limit(body: dict):
    """Create a route policy. Duplicate routes are rejected (409)."""
    if not isinstance(body, dict):
        raise HTTPException(
            status_code=422,
            detail="request body must be a JSON object",
        )

    unknown = set(body) - {"route", "limit", "window", "enabled"}

    if unknown:
        raise HTTPException(
            status_code=422,
            detail="unknown fields: " + ", ".join(sorted(unknown)),
        )

    if "route" not in body:
        raise HTTPException(status_code=422, detail="route is required")

    safe = _policy_route(body["route"], from_url=False)

    try:
        fields = normalize_policy_payload({
            key: value for key, value in body.items()
            if key in ("limit", "window", "enabled")
        })
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    if "limit" not in fields or "window" not in fields:
        missing = [
            name for name in ("limit", "window") if name not in fields
        ]
        raise HTTPException(
            status_code=422,
            detail=f"missing required fields: {', '.join(missing)}",
        )

    try:
        if policy_resolver.get_stored(safe) is not None:
            record_policy_operation("set", "error")
            raise HTTPException(
                status_code=409,
                detail=(
                    f"A policy for {safe!r} already exists; "
                    "use PUT to update it"
                ),
            )

        policy = RoutePolicy(
            route=safe,
            limit=fields["limit"],
            window=fields["window"],
            enabled=fields.get("enabled", True),
        )

        event = new_audit_event(
            "create",
            safe,
            previous_policy=None,
            new_policy=policy.to_dict(),
        )
        _audited_write(
            lambda: policy_resolver.store.set_with_audit(policy, event),
            "create",
        )
        policy_resolver.invalidate(policy.route)
    except HTTPException:
        raise
    except PolicyError:
        record_policy_operation("set", "error")
        raise HTTPException(
            status_code=500,
            detail="Stored policy data is malformed",
        ) from None
    except Exception as exc:
        record_policy_operation("set", "error")
        raise PolicyUnavailable() from exc

    record_policy_operation("set", "success")
    record_policy_audit_event("create", "success")

    return policy.to_dict()


@app.put(
    "/admin/rate-limits/{route:path}",
    dependencies=[Depends(admin_required)]
)
def update_rate_limit(route: str, body: dict):
    """Update an existing route policy (partial updates allowed)."""
    safe = _policy_route(route)

    if not isinstance(body, dict):
        raise HTTPException(
            status_code=422,
            detail="request body must be a JSON object",
        )

    try:
        fields = normalize_policy_payload(body)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    try:
        existing = policy_resolver.get_stored(safe)

        if existing is None:
            record_policy_operation("set", "error")
            raise HTTPException(
                status_code=404,
                detail=f"No dynamic policy for {safe!r}; "
                       "use POST to create one",
            )

        merged = existing.to_dict()
        merged.update(fields)

        policy = RoutePolicy.from_dict(merged)

        event = new_audit_event(
            "update",
            safe,
            previous_policy=existing.to_dict(),
            new_policy=policy.to_dict(),
        )
        _audited_write(
            lambda: policy_resolver.store.set_with_audit(policy, event),
            "update",
        )
        policy_resolver.invalidate(policy.route)
    except HTTPException:
        raise
    except ValueError as exc:
        record_policy_operation("set", "error")
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except PolicyError:
        record_policy_operation("set", "error")
        raise HTTPException(
            status_code=500,
            detail="Stored policy data is malformed",
        ) from None
    except Exception as exc:
        record_policy_operation("set", "error")
        raise PolicyUnavailable() from exc

    record_policy_operation("set", "success")
    record_policy_audit_event("update", "success")

    return policy.to_dict()


@app.delete(
    "/admin/rate-limits/{route:path}",
    status_code=204,
    dependencies=[Depends(admin_required)]
)
def delete_rate_limit(route: str):
    """Remove a dynamic policy; the route returns to its configured
    fallback (RATE_LIMIT_ROUTES or the global default)."""
    safe = _policy_route(route)

    event = new_audit_event("delete", safe)

    try:
        deleted = _audited_write(
            lambda: policy_resolver.store.delete_with_audit(safe, event),
            "delete",
        )
    except Exception as exc:
        record_policy_operation("delete", "error")
        raise PolicyUnavailable() from exc

    if not deleted:
        record_policy_operation("delete", "error")
        raise HTTPException(
            status_code=404,
            detail=f"No dynamic policy for {safe!r}",
        )

    policy_resolver.invalidate(safe)
    record_policy_operation("delete", "success")
    record_policy_audit_event("delete", "success")


@app.exception_handler(PolicyUnavailable)
async def policy_unavailable_handler(request, exc: PolicyUnavailable):
    """Policy storage outages fail clearly (503), never silently."""
    return JSONResponse(
        status_code=503,
        content={"error": "Redis unavailable"},
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

    # Small integration: surface the runtime-managed policies so the
    # playground can display the effective configuration. A store
    # outage simply omits the field (read-only display, not enforcement).
    dynamic_policies = None

    if RATE_LIMIT_BACKEND != "redis":
        dynamic_policies = [
            p.to_dict() for p in policy_resolver.list_policies()
        ]
    else:
        try:
            dynamic_policies = [
                p.to_dict() for p in policy_resolver.list_policies()
            ]
        except Exception:
            dynamic_policies = None

    return {
        "version": "1.1.0",
        "algorithm": algorithm,
        "backend": RATE_LIMIT_BACKEND,
        "limit": limit,
        "window": window,
        "route_limits": route_limits,
        "dynamic_policies": dynamic_policies,
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