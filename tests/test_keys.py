from app.storage.keys import (
    token_bucket_key,
    fixed_window_key,
    sliding_window_key,
    leaky_bucket_key
)


def test_token_bucket_key():
    assert (
        token_bucket_key("user:123")
        == "rateguard:token_bucket:user:123"
    )


def test_fixed_window_key():
    assert (
        fixed_window_key("user:123")
        == "rateguard:fixed_window:user:123"
    )


def test_sliding_window_key():
    assert (
        sliding_window_key("user:123")
        == "rateguard:sliding_window:user:123"
    )


def test_leaky_bucket_key():
    assert (
        leaky_bucket_key("user:123")
        == "rateguard:leaky_bucket:user:123"
    )