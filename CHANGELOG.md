# Changelog

All notable changes to RateGuard are documented here.
The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [1.1.0]

### Added

- Dynamic distributed rate-limit policies: per-route limits manageable at runtime through the admin API (`POST`/`GET`/`PUT`/`DELETE /admin/rate-limits`), stored in Redis when `RATE_LIMIT_BACKEND=redis` so every worker enforces the same configuration; policies survive restarts with Redis data.
- Policy resolution precedence: runtime dynamic policy → `RATE_LIMIT_ROUTES` (static) → global defaults. A disabled dynamic policy falls back to static/global; deleting a policy restores its configured fallback (a route can never become unlimited).
- Bounded policy cache convergence: effective limits are cached per route for 2 seconds, so policy changes apply immediately on the updating worker and within the cache TTL on every other worker — no restarts. Existing rate-limit state is preserved across updates (live limiter re-tuning).
- Policy audit history: one immutable audit event (create / update / delete) recorded per successful admin policy mutation, exposing event id, UTC timestamp, operation, route, before/after policy snapshots and actor (`admin`). Retrieve via `GET /admin/rate-limits/audit?limit=&route=&operation=` (newest first, bounded reads).
- Bounded audit retention via `RATE_LIMIT_AUDIT_MAX_EVENTS` (default `1000`, validated ≥ 1 at startup): Redis mode keeps a single stream trimmed server-side (`XTRIM MAXLEN`); memory mode uses a bounded deque.
- Atomic policy + audit persistence on Redis: the policy write and its audit entry execute as one Lua script, so a mutation can never succeed while its event is silently lost, and the recorded `previous_policy` is always the document that was actually replaced. If recording fails, the whole mutation fails closed (`503`, no state change); ordinary rate-limited requests are unaffected.
- Metrics: `rateguard_policy_updates_total{operation,outcome}` and `rateguard_policy_audit_events_total{operation,outcome}` (bounded labels only).

### Changed

- Runtime policy resolution now backs every rate-limit decision (dynamic policies participate in the request path through the existing ASGI middleware; no additional hot-path Redis reads thanks to the resolver cache).
- Documentation expanded: dynamic-policy lifecycle, audit history semantics, Redis vs memory behavior, and configuration reference.
- Configuration: added `RATE_LIMIT_AUDIT_MAX_EVENTS`; application version metadata consolidated in a single constant.

### Security

- All policy/audit administration endpoints require `X-Admin-Token` matching `ADMIN_API_TOKEN` (constant-time comparison; missing/wrong/unconfigured token → `403`, disabled by default).
- Audit records never contain tokens, API keys, hashes, credentials or client IPs; routes are strictly validated before storage (safe exact-match paths only, preventing Redis key injection).
- Audit retention is hard-bounded; malformed stored policy or audit data surfaces as errors instead of being silently accepted.

## [1.0.0]

### Added

- Four rate-limiting algorithms (fixed window, sliding window, token bucket, leaky bucket), each implemented for in-process memory and Redis backends.
- True ASGI rate-limit middleware as the single enforcement point: per-client and per-route limits, standard `X-RateLimit-*` headers, `429` + `Retry-After`, streaming responses left unbuffered.
- Distributed enforcement across uvicorn workers via atomic Redis Lua scripts using the server clock; TTL cleanup on all state keys; fail-fast startup when Redis is required but unreachable.
- Managed API keys (`rg_live_*`): CSPRNG generation, SHA-256 hash-only storage, one-time secret display, revoke/expire/delete via `/admin/api-keys`.
- Prometheus metrics (`/metrics`) with strictly bounded labels plus a provisioned Grafana dashboard.
- Interactive playground at `/playground` driving the real algorithm implementations.
- Docker Compose deployment (app + Redis + Prometheus + Grafana) and GitHub Actions CI running the full suite against real Redis.

### Security

- Startup validation of all numeric configuration; invalid values abort startup with clear errors.
- Proxy header trust (`X-Forwarded-For`) opt-in via `TRUST_PROXY_HEADERS`; byte-safe constant-time admin token comparison.
