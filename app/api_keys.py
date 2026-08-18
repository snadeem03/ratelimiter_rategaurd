import hashlib
import hmac
import json
import os
import secrets
import threading
from datetime import datetime, timedelta, timezone


def generate_api_key(prefix: str = "rg_live_") -> str:
    """Generate a new API key using a cryptographically secure RNG."""
    return f"{prefix}{secrets.token_urlsafe(32)}"


def hash_api_key(api_key: str) -> str:
    """Return the SHA-256 hex digest of an API key.

    Only this digest is ever stored; the plaintext key is never
    persisted or logged.
    """
    return hashlib.sha256(api_key.encode("utf-8")).hexdigest()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _metadata(record: dict) -> dict:
    """Public metadata for a key record. Never includes the secret or hash."""
    return {
        "id": record["hash"],
        "name": record["name"],
        "enabled": bool(record["enabled"]),
        "owner": record.get("owner"),
        "created_at": record.get("created_at"),
        "expires_at": record.get("expires_at"),
    }


def _is_active(record: dict) -> bool:
    if not record.get("enabled"):
        return False

    expires_at = record.get("expires_at")

    if expires_at:
        expiration = datetime.fromisoformat(expires_at)

        if datetime.now(timezone.utc) > expiration:
            return False

    return True


class ApiKeyStore:
    """File-backed API key store (pure in-memory when ``path`` is None).

    Keys are stored as SHA-256 digests. The store is used with the
    ``memory`` rate-limit backend.
    """

    def __init__(self, path: str = None, prefix: str = "rg_live_"):
        self.path = path
        self.prefix = prefix
        self._records = {}
        self._lock = threading.Lock()

        if path:
            self._load()

    def _load(self):
        if os.path.exists(self.path):
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)

            self._records = data.get("api_keys", {})

    def _persist(self):
        if not self.path:
            return

        tmp_path = f"{self.path}.tmp"

        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump({"api_keys": self._records}, f, indent=2)

        os.replace(tmp_path, self.path)

    def create(
        self,
        name: str,
        owner: str = None,
        ttl: int = None,
    ):
        """Create a new key. Returns (plaintext_key, metadata).

        The plaintext key is returned exactly once, at creation time.
        """
        api_key = generate_api_key(self.prefix)
        key_hash = hash_api_key(api_key)
        created_at = _now()

        expires_at = None
        if ttl is not None:
            expires_at = (
                datetime.now(timezone.utc) + timedelta(seconds=ttl)
            ).isoformat()

        record = {
            "hash": key_hash,
            "name": name,
            "enabled": True,
            "owner": owner,
            "created_at": created_at,
            "expires_at": expires_at,
        }

        with self._lock:
            self._records[key_hash] = record
            self._persist()

        return api_key, _metadata(record)

    def verify(self, api_key: str):
        """Return the record for ``api_key`` if it exists, else None."""
        key_hash = hash_api_key(api_key)

        with self._lock:
            record = self._records.get(key_hash)

        if record is None:
            return None

        if not hmac.compare_digest(record["hash"], key_hash):
            return None

        return record

    def authenticate(self, api_key: str):
        """Return the record if the key exists and is active, else None."""
        record = self.verify(api_key)

        if record is None:
            return None

        if not _is_active(record):
            return None

        return record

    def list(self) -> list:
        with self._lock:
            return [_metadata(record) for record in self._records.values()]

    def revoke(self, key_id: str) -> bool:
        """Disable a key by id. Returns True if a key was found."""
        with self._lock:
            record = self._records.get(key_id)

            if record is None:
                return False

            record["enabled"] = False
            self._persist()

        return True

    def delete(self, key_id: str) -> bool:
        """Permanently remove a key by id. Returns True if removed."""
        with self._lock:
            if key_id not in self._records:
                return False

            del self._records[key_id]
            self._persist()

        return True


class RedisApiKeyStore:
    """Redis-backed API key store, for the ``redis`` rate-limit backend.

    Each key is a Redis hash ``rateguard:apikey:{hash}`` holding the
    record fields; an index set ``rateguard:apikeys`` tracks all hashes.
    """

    INDEX_KEY = "rateguard:apikeys"

    def __init__(self, redis_client, prefix: str = "rg_live_"):
        self.redis_client = redis_client
        self.prefix = prefix

    @staticmethod
    def _record_key(key_hash: str) -> str:
        return f"rateguard:apikey:{key_hash}"

    @staticmethod
    def _normalize(key_hash: str, data: dict) -> dict:
        return {
            "hash": key_hash,
            "name": data.get("name"),
            "enabled": data.get("enabled") == "true",
            "owner": data.get("owner") or None,
            "created_at": data.get("created_at"),
            "expires_at": data.get("expires_at") or None,
        }

    def create(
        self,
        name: str,
        owner: str = None,
        ttl: int = None,
    ):
        api_key = generate_api_key(self.prefix)
        key_hash = hash_api_key(api_key)
        created_at = _now()

        expires_at = ""
        if ttl is not None:
            expires_at = (
                datetime.now(timezone.utc) + timedelta(seconds=ttl)
            ).isoformat()

        record = {
            "name": name,
            "enabled": "true",
            "owner": owner or "",
            "created_at": created_at,
            "expires_at": expires_at,
        }

        key = self._record_key(key_hash)

        pipe = self.redis_client.pipeline()
        pipe.hset(key, mapping=record)
        pipe.sadd(self.INDEX_KEY, key_hash)
        if ttl is not None:
            pipe.expire(key, max(ttl, 1))
        pipe.execute()

        return api_key, _metadata(self._normalize(key_hash, record))

    def _get(self, key_hash: str):
        data = self.redis_client.hgetall(self._record_key(key_hash))

        if not data:
            return None

        return self._normalize(key_hash, data)

    def verify(self, api_key: str):
        key_hash = hash_api_key(api_key)
        record = self._get(key_hash)

        if record is None:
            return None

        if not hmac.compare_digest(record["hash"], key_hash):
            return None

        return record

    def authenticate(self, api_key: str):
        record = self.verify(api_key)

        if record is None:
            return None

        if not _is_active(record):
            return None

        return record

    def list(self) -> list:
        key_hashes = self.redis_client.smembers(self.INDEX_KEY)

        records = [
            _metadata(record)
            for key_hash in key_hashes
            if (record := self._get(key_hash)) is not None
        ]

        return sorted(records, key=lambda record: record["created_at"])

    def revoke(self, key_id: str) -> bool:
        key = self._record_key(key_id)

        if not self.redis_client.exists(key):
            return False

        self.redis_client.hset(key, "enabled", "false")

        return True

    def delete(self, key_id: str) -> bool:
        pipe = self.redis_client.pipeline()
        pipe.delete(self._record_key(key_id))
        pipe.srem(self.INDEX_KEY, key_id)

        return bool(pipe.execute()[0])