# RateGuard

A rate-limiting service built with **FastAPI + Redis**. It throttles clients using pluggable algorithms — **fixed window, sliding window, token bucket, leaky bucket** — each available with an in-memory (`memory`) or shared Redis (`redis`) backend. Rate limits are enforced per client (keyed by `X-API-Key` or client IP).

## Run

```powershell
venv\Scripts\Activate.ps1
pip install -r requirements.txt
uvicorn app.main:app --reload            # http://127.0.0.1:8000
```

Configurable via `.env`: `RATE_LIMIT_ALGORITHM`, `RATE_LIMIT`, `RATE_LIMIT_WINDOW`, `RATE_LIMIT_BACKEND` (`memory` | `redis`), `TRUST_PROXY_HEADERS`, `REDIS_URL`, and Redis timeouts.

## Endpoints

- `GET /` — health/info
- `GET /api/test` — rate-limited test endpoint

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