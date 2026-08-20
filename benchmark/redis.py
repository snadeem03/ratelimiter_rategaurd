"""Redis client construction for benchmarks.

Settings come from the same environment variables RateGuard uses in
``app/core/redis_client.py``; nothing is hardcoded. An explicit
``--redis-url`` (CLI) overrides ``REDIS_URL``.
"""

import os

import redis


def build_redis_client(url: str | None = None) -> redis.Redis:
    """Build a Redis client from RateGuard's environment configuration."""
    url = url or os.getenv("REDIS_URL", "redis://localhost:6379/0")
    password = os.getenv("REDIS_PASSWORD") or None
    socket_timeout = float(os.getenv("REDIS_SOCKET_TIMEOUT", "2"))
    socket_connect_timeout = float(
        os.getenv("REDIS_SOCKET_CONNECT_TIMEOUT", "2")
    )
    health_check_interval = int(
        os.getenv("REDIS_HEALTH_CHECK_INTERVAL", "30")
    )

    return redis.from_url(
        url,
        password=password,
        socket_timeout=socket_timeout,
        socket_connect_timeout=socket_connect_timeout,
        health_check_interval=health_check_interval,
        decode_responses=True,
    )


def redis_available(client) -> bool:
    """Return True when the client can reach a live Redis server."""
    try:
        return bool(client.ping())
    except Exception:
        return False