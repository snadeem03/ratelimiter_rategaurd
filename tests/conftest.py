"""Shared pytest fixtures.

Session-scoped cleanup of the ``rateguard:*`` Redis namespace: several
test modules drive real HTTP requests through the Redis-backed limiter
and only clean their own key markers, which previously left up to ~60
limiter keys behind after a full run. Leftovers expire via TTL but could
contaminate a re-run started within the TTL window (e.g. ``ip:testclient``
budget already consumed). Sweeping before and after the session keeps
runs order- and repetition-independent. Skips silently when no Redis
server is available.
"""

import pytest

from app.core.redis_client import get_redis


def _clear_rateguard_keys():
    try:
        client = get_redis()
        client.ping()
    except Exception:
        return

    for key in client.scan_iter("rateguard:*", count=500):
        client.delete(key)


@pytest.fixture(autouse=True, scope="session")
def clean_rateguard_redis_keys():
    _clear_rateguard_keys()
    yield
    _clear_rateguard_keys()
