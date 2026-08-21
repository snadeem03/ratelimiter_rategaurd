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

### Docker

Run the **complete application**, including Redis, with a single command:

```powershell
docker compose up --build
```

Then open:

- **http://localhost:8000** — the API (`GET /` health/info)
- **http://localhost:8000/playground** — the interactive RateGuard Playground
- **http://localhost:8000/docs** — OpenAPI docs

Wait for both services to report **healthy** first (`docker compose ps`). Compose only starts RateGuard after the Redis health check passes, so the app's fail-fast Redis startup check succeeds immediately.

#### Services

| Service   | Image                 | Published port | Role                                     |
|-----------|-----------------------|----------------|------------------------------------------|
| `rategaurd` | built from `Dockerfile` | `8000:8000`    | FastAPI app, 2 uvicorn workers          |
| `redis`   | `redis:7-alpine`      | *(none)*       | shared rate-limit state, not public      |

Redis is **not exposed to the host** — it is only reachable on the Compose network under the service name `redis`. If you need it for local debugging, expose it explicitly (not recommended for production).

#### Environment configuration

Compose supplies the Redis backend automatically, with sensible defaults you can override by exporting the same variables before `docker compose up` (or placing them in a gitignored `.env` — see `.env.example`):

- `RATE_LIMIT_BACKEND=redis`
- `REDIS_URL=redis://redis:6379/0` (service name, never `localhost`)
- `RATE_LIMIT_ALGORITHM`, `RATE_LIMIT`, `RATE_LIMIT_WINDOW`, `RATE_LIMIT_ROUTES`, `TRUST_PROXY_HEADERS`
- `ADMIN_API_TOKEN` — **must be supplied via environment** to enable `/admin/api-keys`; unset disables the admin API (403). Never hardcode it and never commit it.

`RATE_LIMIT_BACKEND=redis` makes all 2 uvicorn workers share the same rate-limit state (the existing Redis implementations), and preserves the **fail-fast** behaviour: if Redis is unreachable at startup the app raises instead of silently falling back to memory.

#### Health checks

- **Redis** — `redis-cli ping` every 5s; RateGuard waits for `service_healthy` before starting.
- **RateGuard** — HTTP `GET /` (excluded from rate limiting) every 30s.

#### Stop and logs

```powershell
docker compose down
```

```powershell
docker compose logs -f            # both services
docker compose logs -f rategaurd  # application only
docker compose logs -f redis      # redis only
```

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

## Benchmarking

The `benchmark/` package measures the four rate-limiting algorithms on both
backends. It drives the **real** limiter implementations (no mocks) and saves
results to `benchmark/results/` (CSV + JSON).

```powershell
# in-memory benchmark of all four algorithms (burst traffic)
python -m benchmark.benchmark

# Redis-backed benchmark, all algorithms
python -m benchmark.benchmark --backend redis

# both backends, three traffic patterns, concurrency 1 / 10 / 50
python -m benchmark.benchmark --backend both --traffic all --concurrency 1,10,50

# a single scenario
python -m benchmark.benchmark --backend redis --algorithm token_bucket --traffic burst --requests 1000 --concurrency 10
```

### Flags

| Flag | Default | Description |
| ---- | ------- | ----------- |
| `--backend` | `memory` | `memory`, `redis`, or `both` |
| `--algorithm` | `all` | `all` or `fixed_window`, `sliding_window`, `token_bucket`, `leaky_bucket` |
| `--traffic` | `burst` | `all`, `normal`, `burst`, or `sustained` |
| `--requests` | `1000` | total requests per scenario |
| `--concurrency` | `1` | comma-separated worker counts, or `all` for `1,10,50` |
| `--limit` / `--window` | `100` / `60` | the rate-limit configuration applied to every algorithm |
| `--interval` | auto | seconds between requests for paced traffic |
| `--redis-url` | env | override `REDIS_URL` |
| `--format` | `all` | `table` (print only), `csv`, `json`, or `all` |

### Traffic patterns

- **burst** — rapid-fire requests with no spacing; the limit is hit immediately
  and subsequent requests are rejected.
- **normal** — requests spaced at *half* the sustainable rate
  (`window / limit * 2` seconds), so nearly everything is allowed.
- **sustained** — requests paced right at the sustainable rate
  (`window / limit` seconds), producing a mix of allowed and rejected.

For paced patterns each run takes roughly `requests * interval` seconds;
use smaller `--requests` or set `--interval` explicitly to keep runs short.

### Concurrency

`--concurrency 1,10,50` runs each scenario with 1, 10, and 50 concurrent
workers. These are **benchmark scenarios, not universally representative
deployments**:

- memory backend: all workers share one limiter instance (lock contention).
- Redis backend: each worker gets its own limiter instance but they share the
  same Redis keys, mirroring a multi-worker uvicorn deployment. Results include
  Redis connection-pool and Lua-script contention.

### Correctness before measurement

Before timing, every scenario verifies on a fresh limiter that exactly
`limit` requests pass and the next ones are rejected, so the benchmark never
bypasses the configured limit. Burst runs additionally assert the total
allowed requests never exceed `limit`.

### Measured metrics

| Metric | Meaning |
| ------ | ------- |
| `Requests` / `Allowed` / `Rejected` | request totals |
| `RPS` | requests per second over wall-clock elapsed time |
| `Avg / P50 / P95 / P99 (ms)` | per-request `allow_request()` latency percentiles |

### Redis isolation

Every run uses unique keys (`rateguard:{algorithm}:bench:{run_id}:...`) and
deletes them afterwards, so no state leaks between scenarios or runs.

### Redis availability

- `--backend redis` with no reachable server **fails clearly** (exit code 1).
- `--backend both` with no reachable server prints an explicit message and runs
  memory-only. There is no silent fallback for an explicitly requested Redis
  benchmark.

### Caveats

Comparisons are only indicative, not authoritative:

- Algorithm semantics differ (fixed window re-opens each window, sliding window
  is continuous, token bucket refills continuously, leaky bucket drains FIFO).
- Redis results include network round-trips and connection-pool effects.
- **Results depend on your machine, Python version, and Redis deployment**
  (single-node vs clustered, network latency, CPU). Treat numbers as relative
  comparisons on the same host, not absolute guarantees.

## RateGuard Playground

A local, interactive playground for visually exploring the **real** RateGuard
rate-limiting algorithms. It is a developer/testing tool, not a production
dashboard — no authentication, no remote backend, nothing leaves your machine.

### 1. Start RateGuard

```powershell
venv\Scripts\Activate.ps1
pip install -r requirements.txt
uvicorn app.main:app --reload        # http://127.0.0.1:8000
```

Redis is optional for the playground. It is only needed for the **Redis**
backend options (simulation backend and Redis-backed live API).

### 2. Open the playground

Browse to **http://127.0.0.1:8000/playground**. The page is served by the same
RateGuard app, so everything runs on your machine with no CORS or external
services.

### 3. Simulation mode (default)

Simulation drives the **actual RateGuard algorithm implementations** server-side
through the rate-limiter factory — the browser never re-implements rate
limiting, so what you see is exactly what the running API enforces.

- Pick an **algorithm**, a **limit** and a **window**; select **Memory** (no
  Redis needed) or **Redis** (uses the real Redis-backed algorithms; requires a
  reachable server — otherwise you get **Redis unavailable** and requests fail,
  there is no silent memory fallback).
- Choose a **client ID** (the identity that keys the limit) and a **route**.
- **Send 1**, **Send 5**, or **Burst** (overshoots the limit to show 429s);
  **Start auto** sends one request on a timer; **Reset** recreates the limiter
  and clears the log.

Because simulation uses the real algorithms, bursts behave exactly like the
live API: the configured number of requests pass and the rest are rejected.

### 4. Live API mode

Switch to **Live API** to send real HTTP requests through RateGuard's ASGI
rate-limit middleware. It reads the `X-RateLimit-Limit`, `X-RateLimit-Remaining`,
`X-RateLimit-Reset` and `Retry-After` headers from every real response and
visualizes them, including the real 429 rejection.

- The API base defaults to the same origin (`http://127.0.0.1:8000`); you can
  point it elsewhere, but then the target must allow CORS.
- An optional **X-API-Key** is sent on each request. Managed keys
  (`rg_live_*`) are authenticated against the existing store; any other value
  acts as an opaque client identity — the same behaviour as the real API. Keys
  are never persisted, stored in `localStorage`, or logged.
- Live mode shows the **server's actual configuration** (algorithm, backend,
  limit, window, per-route limits) read-only. To change it, edit `.env` and
  restart — the playground never modifies your configuration.

### 5. Memory vs Redis

- **Memory** — per-process limiters (simulation) / whatever the server runs
  (live).
- **Redis** — the real Redis-backed implementations shared across workers (live
  mode requires the server to run `RATE_LIMIT_BACKEND=redis`). A badge in the
  header always reports Redis reachability; an unavailable Redis is shown as
  **Redis unavailable**, never silently downgraded.

### 6. Algorithm selection

Each algorithm gets its own visualization:

- **Fixed Window** — a counter with per-request slots, a window progress bar,
  countdown, and an animated window reset when the window expires.
- **Sliding Window** — a live timeline; requests appear at "now" and slide left
  until they pass the expiry boundary and disappear.
- **Token Bucket** — a bucket that refills continuously (tokens animate back in)
  and drains one token per allowed request; an empty bucket rejects with a shake.
- **Leaky Bucket** — a FIFO queue that fills from the top and drains at a
  constant rate out the bottom; a full bucket rejects at the inlet.

The request flow strip (`Client → Middleware → Limiter → Result`) pulses on every
request, and the live request log records each event with status, remaining,
reset, route, client, and full 429 header details.

### 7. What the visualization represents

Every metric is real: remaining, reset, allowed/rejected counts, rate and
success percentage come from the live algorithm state or the actual response
headers — nothing is fabricated. Animations respect `prefers-reduced-motion`
and are purely presentational; the underlying numbers always come from
RateGuard itself.