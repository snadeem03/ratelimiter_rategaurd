# RateGuard

A rate-limiting service built with **FastAPI + Redis**. It throttles clients using pluggable algorithms — **fixed window, sliding window, token bucket, leaky bucket** — each available with an in-memory (`memory`) or shared Redis (`redis`) backend. Rate limits are enforced per client (keyed by `X-API-Key` or client IP).

## Architecture

Rate-limit enforcement happens once, in a reusable **ASGI middleware layer** (`app/middleware/rate_limit_middleware.py`), before requests reach the endpoints. Endpoints no longer call the limiter themselves — the middleware is the single enforcement point:

```
ASGI Middleware (RateLimitMiddleware)
      ↓
Client / route resolution      # API-key / IP identity + request path
      ↓
RateLimiter facade             # per-(route, client) algorithm instances
      ↓
Algorithm                      # fixed/sliding window, token/leaky bucket
      ↓
Memory OR Redis backend        # atomic Lua scripts shared across uvicorn workers
```

The middleware inspects each HTTP request, resolves the client identity (`X-API-Key` or IP), applies the configured per-route limit (or the global fallback), and rejects over-limit requests with `429`. Every allowed response automatically receives the standard `X-RateLimit-*` headers, and over-limit responses include `Retry-After`. The root, docs, and OpenAPI paths (`/`, `/docs`, `/redoc`, `/openapi.json`) and the admin API (`/admin/...`) are never rate-limited.

## Run

```powershell
venv\Scripts\Activate.ps1
pip install -r requirements.txt
uvicorn app.main:app --reload            # http://127.0.0.1:8000
```

Configurable via `.env`: `RATE_LIMIT_ALGORITHM`, `RATE_LIMIT`, `RATE_LIMIT_WINDOW`, `RATE_LIMIT_ROUTES`, `RATE_LIMIT_BACKEND` (`memory` | `redis`), `TRUST_PROXY_HEADERS`, `API_KEY_PREFIX`, `API_KEY_STORE_PATH`, `ADMIN_API_TOKEN`, `REDIS_URL`, and Redis timeouts.

## Endpoints

- `GET /` — health/info
- `GET /api/test` — rate-limited test endpoint
- `POST /api/login` — rate-limited login endpoint
- `GET /api/products` — rate-limited products endpoint
- `POST /api/orders` — rate-limited orders endpoint

## Per-route rate limits

By default every route shares the global `RATE_LIMIT` / `RATE_LIMIT_WINDOW`. To give specific routes their own limits, set `RATE_LIMIT_ROUTES` as a comma-separated list of `path:limit:window` entries:

```dotenv
# GET /api/test        -> 100 requests/minute
# POST /api/login      -> 10 requests/minute
# GET /api/products    -> 200 requests/minute
# POST /api/orders     -> 30 requests/minute
RATE_LIMIT_ROUTES=/api/test:100:60,/api/login:10:60,/api/products:200:60,/api/orders:30:60
```

Routes not listed in `RATE_LIMIT_ROUTES` fall back to the global `RATE_LIMIT` and `RATE_LIMIT_WINDOW`.

Limits are still enforced **per client**: the effective key is `route + client`, so the limit for `/api/login` never interferes with `/api/products`, and two clients never share a budget. With the Redis backend the key is `rateguard:{algorithm}:{route}:{client}`, so limits stay correct across uvicorn workers.

## Leaky bucket (Redis backend)

Select the distributed leaky bucket with `RATE_LIMIT_ALGORITHM=leaky_bucket` and `RATE_LIMIT_BACKEND=redis`. The bucket is stored in Redis so all uvicorn workers share the same state:

- **State** — admitted requests live in a sorted set per `rateguard:leaky_bucket:{route}:{client}` key; a per-bucket counter (`:seq`) keeps members unique and a `:last_leak` timestamp drives the drain. Timestamps come from `redis.call("TIME")` (the Redis server clock), so workers agree regardless of client clock skew.
- **Leak rate** — the bucket drains `limit / RATE_LIMIT_WINDOW` requests per second in FIFO order (one slot frees every `RATE_LIMIT_WINDOW / limit` seconds). Bursts up to `capacity` pass immediately; a full bucket rejects with `429` until the next slot drains.
- **Atomicity** — admission and leak run in a single Lua script, so concurrent requests cannot oversubscribe the last slot.
- **Expiry** — every state key gets a TTL equal to the full-drain time (`capacity / leak_rate`, refreshed on each request), so buckets abandoned by idle clients never linger in Redis.

## API keys

RateGuard can authenticate clients with API keys sent via the `X-API-Key` header.

### How keys work

- Keys are generated with a cryptographically secure RNG and carry a `rg_live_` prefix (configurable via `API_KEY_PREFIX`).
- Only a **SHA-256 digest** of the key is stored; the plaintext key is shown exactly once, at creation time, and is never logged or returned by any list/get endpoint.
- The rate-limit identity for a key is its stable **client identity**: the key's `owner` if set, otherwise its digest. Two keys with different owners get independent quotas; two keys sharing an `owner` share a quota.

```powershell
# create a key
curl -X POST http://127.0.0.1:8000/admin/api-keys `
  -H "Content-Type: application/json" `
  -d '{"name":"my-app","owner":"acme"}'

# response (the only time the secret is shown)
# {"key":"rg_live_XXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX","id":"...","name":"my-app","enabled":true,"owner":"acme","created_at":"...","expires_at":null}
```

### Usage

```http
GET /api/products HTTP/1.1
X-API-Key: rg_live_XXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX
```

### Authentication behavior

- **Valid, enabled, non-expired key** → used as the primary client identity for rate limiting.
- **No `X-API-Key` header** → falls back to the client IP (or `X-Forwarded-For` when `TRUST_PROXY_HEADERS=true`). Existing clients that don't use API keys are unaffected.
- **`X-API-Key` supplied but invalid / disabled / expired** → `401 Unauthorized`. There is no silent IP fallback for an explicitly supplied key that fails validation.
- Header values that do **not** match the managed `rg_live_` prefix keep the legacy opaque-client behaviour.

### Revocation

All `/admin/api-keys` endpoints are protected by an **admin token**. Set the `ADMIN_API_TOKEN` environment variable and send it with every admin request:

```http
X-Admin-Token: <your-admin-token>
```

When `ADMIN_API_TOKEN` is not set, the admin API is disabled and every admin request is rejected with `403` — nothing is publicly accessible by default. The token is compared in constant time and is never logged or returned by any endpoint.

```powershell
# disable a key (it can no longer authenticate)
curl -X POST http://127.0.0.1:8000/admin/api-keys/{id}/revoke -H "X-Admin-Token: $env:ADMIN_API_TOKEN"

# permanently delete a key
curl -X DELETE http://127.0.0.1:8000/admin/api-keys/{id} -H "X-Admin-Token: $env:ADMIN_API_TOKEN"

# list key metadata (never includes the secret)
curl http://127.0.0.1:8000/admin/api-keys -H "X-Admin-Token: $env:ADMIN_API_TOKEN"
```

The key `id` is an opaque identifier returned by `POST /admin/api-keys` and `GET /admin/api-keys`; it is not the secret and cannot be used to authenticate.

### Storage

- Default (memory backend): a local JSON file at `API_KEY_STORE_PATH` (default `api_keys.json`, gitignored). Set `API_KEY_STORE_PATH` to relocate it.
- Redis backend (`RATE_LIMIT_BACKEND=redis`): keys are stored in Redis hashes (`rateguard:apikey:{hash}`) so they work across uvicorn workers.

## Rate limit response headers

Every response from a rate-limited endpoint includes the standard headers:

| Header                  | Description                                        |
| ----------------------- | -------------------------------------------------- |
| `X-RateLimit-Limit`     | Maximum requests allowed per window                |
| `X-RateLimit-Remaining` | Requests still available in the current window     |
| `X-RateLimit-Reset`     | Seconds until the limit resets                     |

### 429 responses

When a client exceeds its limit the API responds with `429 Too Many Requests` and:

- the same `X-RateLimit-*` headers (`X-RateLimit-Remaining` is `0`, never negative),
- an RFC 6585 `Retry-After` header set to the same value as `X-RateLimit-Reset` (seconds until a request will be allowed again),
- the reset value also in the response body as `detail.retry_after`.