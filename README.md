# RateGuard

A rate-limiting service built with **FastAPI + Redis**. It throttles clients using pluggable algorithms — **fixed window, sliding window, token bucket, leaky bucket** — each available with an in-memory (`memory`) or shared Redis (`redis`) backend. Rate limits are enforced per client (keyed by `X-API-Key` or client IP).

## Run

```powershell
venv\Scripts\Activate.ps1
pip install -r requirements.txt
uvicorn app.main:app --reload            # http://127.0.0.1:8000
```

Configurable via `.env`: `RATE_LIMIT_ALGORITHM`, `RATE_LIMIT`, `RATE_LIMIT_WINDOW`, `RATE_LIMIT_ROUTES`, `RATE_LIMIT_BACKEND` (`memory` | `redis`), `TRUST_PROXY_HEADERS`, `REDIS_URL`, and Redis timeouts.

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