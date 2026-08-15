from app.core.redis_client import get_redis
from app.storage.redis_storage import RedisStorage


def test_storage_set_and_get():
    storage = RedisStorage(get_redis())

    key = "rateguard:test"

    storage.set(key, "hello")

    assert storage.get(key) == "hello"

    storage.delete(key)


def test_storage_exists():
    storage = RedisStorage(get_redis())

    key = "rateguard:test"

    storage.set(key, "hello")

    assert storage.exists(key) is True

    storage.delete(key)


def test_storage_expiration():
    storage = RedisStorage(get_redis())

    key = "rateguard:test"

    storage.set(
        key,
        "hello",
        expiration=10
    )

    assert storage.get(key) == "hello"

    storage.delete(key) 