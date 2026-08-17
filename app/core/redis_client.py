import os

import redis


REDIS_URL = os.getenv(
    "REDIS_URL",
    "redis://localhost:6379/0"
)

REDIS_PASSWORD = os.getenv(
    "REDIS_PASSWORD"
)

REDIS_SOCKET_TIMEOUT = float(
    os.getenv(
        "REDIS_SOCKET_TIMEOUT",
        "2"
    )
)

REDIS_SOCKET_CONNECT_TIMEOUT = float(
    os.getenv(
        "REDIS_SOCKET_CONNECT_TIMEOUT",
        "2"
    )
)

REDIS_HEALTH_CHECK_INTERVAL = int(
    os.getenv(
        "REDIS_HEALTH_CHECK_INTERVAL",
        "30"
    )
)


redis_client = redis.from_url(
    REDIS_URL,
    password=REDIS_PASSWORD or None,
    socket_timeout=REDIS_SOCKET_TIMEOUT,
    socket_connect_timeout=REDIS_SOCKET_CONNECT_TIMEOUT,
    health_check_interval=REDIS_HEALTH_CHECK_INTERVAL,
    decode_responses=True
)


def get_redis():
    return redis_client