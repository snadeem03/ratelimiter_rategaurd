from typing import Optional

import redis


class RedisStorage:
    def __init__(self, client: redis.Redis):
        self.client = client

    def get(self, key: str) -> Optional[str]:
        return self.client.get(key)

    def set(
        self,
        key: str,
        value: str,
        expiration: Optional[int] = None
    ) -> None:

        if expiration is not None:
            self.client.set(
                key,
                value,
                ex=expiration
            )
        else:
            self.client.set(
                key,
                value
            )

    def delete(self, key: str) -> None:
        self.client.delete(key)

    def exists(self, key: str) -> bool:
        return bool(
            self.client.exists(key)
        )