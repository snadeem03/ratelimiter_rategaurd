# RateGuard v1.0 Release-Hardening Audit Report

**Scope:** Full review of all 20 app modules, 21 test files, CI workflow (`.github/workflows/ci.yml`), Docker/Compose/observability configs, README, and git history.
**Result:** Ready for release. No blocking issues remain after fixes.

---

## 1. Audit summary

Full review across security, concurrency, Redis failure behavior, API-key handling, admin API protection, ASGI middleware correctness, configuration validation, Docker security, observability, CI, test reliability, resource cleanup, documentation accuracy, and dependencies.

Verified as **already correct — no changes made**:

- API-key generation (`secrets.token_urlsafe(32)`), hash-only persistence, plaintext shown once at creation
- Admin API disabled by default (403 when `ADMIN_API_TOKEN` unset), constant-time token comparison
- Redis Lua scripts atomic (single round-trip returns `{allowed, remaining, reset}`), server-clock timestamps, TTLs on all state keys incl. `:seq` / `:last_leak`, cached read paths
- Fail-fast startup ping when `RATE_LIMIT_BACKEND=redis` (no silent fallback)
- ASGI middleware: streaming responses never buffered, header merge on `http.response.start` only, 429 with `Retry-After` + body, excluded paths/prefixes, 401 from identity resolution handled
- `/metrics` never rate-limited; metric labels strictly bounded (no API keys / client IDs / IPs)
- Docker: non-root uid 10001, only `app/` in image, Redis internal-only (no published ports), healthcheck ordering via `depends_on: condition: service_healthy`
- No CORS middleware configured, no `eval`, no path traversal (Starlette `StaticFiles` defaults safe), no secrets in git history

## 2–4. Issues found → severity → fixes

| # | Issue | Severity | Fix |
|---|---|---|---|
| 1 | `window=0`/`limit=0` accepted: `ZeroDivisionError` at **first request** (token/leaky bucket); fixed/sliding windows silently misbehave; `RATE_LIMIT_ROUTES` accepted `0`/negatives | **Medium** (config robustness) | Startup validation in `app/main.py` (`_env_int`, ≥1 checks naming the env var) + positivity validation in `parse_route_limits`; non-int values raise errors naming the variable |
| 2 | Non-ASCII `X-Admin-Token` → `hmac.compare_digest` raises `TypeError` → unhandled **500** instead of 403 (raw header bytes reach uvicorn; httpx refuses to send them) | **Low-Medium** | Compare UTF-8-encoded bytes in `app/main.py::admin_required` |
| 3 | Error message showed literal `{entry!r}` — missing `f` prefix in `parse_route_limits` | Low | Added prefix |
| 4 | Full suite left **60 leftover `rateguard:*` keys** in Redis (several modules clean only their own markers); rerun within TTL (~65s) could inherit consumed budgets (`ip:testclient`) — order/repetition dependence | **Medium** (test reliability) | New `tests/conftest.py`: session-scoped sweep of `rateguard:*` before+after suite (silent no-op without Redis) → now **0 leftovers** |
| 5 | Playground sim-session registry unbounded; endpoint is unauthenticated + excluded from rate limiting → memory-growth vector | Low | `MAX_SESSIONS = 100`, LRU eviction closes oldest session (+ its Redis keys) |
| 6 | Playground reset during Redis outage → raw `RedisError` → 500, inconsistent with designed 503 elsewhere | Low | `SimSession.reset()` converts to `RedisUnavailable`; endpoint returns 503 |
| 7 | `aiofiles`, `python-multipart` in requirements but imported nowhere | Low (deps) | Removed from `requirements.txt`; proven sufficient by fresh image build |

## 5. Tests added (14 new)

- `tests/test_startup_validation.py` (6): subprocess `import app.main` with zero/negative/non-int globals + bad routes → nonzero exit, env var named
- `tests/test_route_limits.py` (+5): zero/negative entries rejected; error message interpolates the entry (regression for #3)
- `tests/test_api_keys.py` (+1): non-ASCII admin token → 403 not crash (regression for #2)
- `tests/test_playground.py` (+2): reset→503 on Redis outage; registry cap evicts LRU session

## 6. Full pytest result

**323 passed** (was 309), 0 skipped, 0 failed, 22.2s — against a real Redis server; **0 leftover keys after the run**. Only pre-existing Starlette/httpx `TestClient` deprecation warning remains.

## 7. Docker validation

- `docker compose config --quiet` ✓
- `docker compose build rategaurd` ✓ (fresh build validates trimmed requirements)

No compose/Dockerfile changes needed; audit confirmed non-root user, internal-only Redis, healthcheck ordering all correct.

## 8. CI validation

Workflow unchanged; re-validated with actionlint (**exit 0**). CI runs the full suite against a real Redis service container and fails on any failure or Redis-skip guard — unaffected by these changes.

## 9. Documentation changes

README: one paragraph added under *Run* documenting the new startup-validation behavior. All other sections verified accurate against implementation.

## 10. Remaining known limitations (not fixed, by design)

- Legacy opaque `X-API-Key` values appear verbatim inside limiter key names (managed `rg_live_*` keys are always hashed/owner-based); theoretical route/client separator (`:`) collisions share a bucket.
- Memory-backend limiter recreation under extreme churn (>10k concurrent identities) can reset counts — inherent to documented LRU design.
- Redis outage mid-request → fail-closed 500s (loud, not silent allow).
- ≤1s difference between memory/Redis `X-RateLimit-Reset` rounding.
- Grafana `admin/admin` local-dev default (documented).

## 11. Recommended v1.0 release status

**Ready for release.**

## 12. Suggested commit message

```
fix: harden config validation, admin token compare, and test cleanup for v1.0
```

---

*Files changed:* `app/config.py`, `app/main.py`, `app/playground/simulation.py`, `requirements.txt`, `README.md`, `tests/test_api_keys.py`, `tests/test_playground.py`, `tests/test_route_limits.py`, plus new `tests/conftest.py` and `tests/test_startup_validation.py`.
