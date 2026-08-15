def token_bucket_key(client_id: str) -> str:
    return f"rateguard:token_bucket:{client_id}"


def fixed_window_key(client_id: str) -> str:
    return f"rateguard:fixed_window:{client_id}"


def sliding_window_key(client_id: str) -> str:
    return f"rateguard:sliding_window:{client_id}"


def leaky_bucket_key(client_id: str) -> str:
    return f"rateguard:leaky_bucket:{client_id}"