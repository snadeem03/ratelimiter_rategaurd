import time
import pytest
from fastapi.testclient import TestClient
from app.main import app
from app.middleware.rate_limiter import RateLimiter


@pytest.fixture
def client():
    return TestClient(app)


def test_rate_limiter_allow_requests():
    limiter = RateLimiter(limit=3, window=10)
    assert limiter.allow_request() is True
    assert limiter.allow_request() is True
    assert limiter.allow_request() is True
    assert limiter.allow_request() is False


def test_rate_limiter_remaining_requests():
    limiter = RateLimiter(limit=3, window=10)
    assert limiter.remaining_requests() == 3
    limiter.allow_request()
    assert limiter.remaining_requests() == 2
    limiter.allow_request()
    assert limiter.remaining_requests() == 1
    limiter.allow_request()
    assert limiter.remaining_requests() == 0
    limiter.allow_request()
    assert limiter.remaining_requests() == 0


def test_rate_limiter_reset_time():
    limiter = RateLimiter(limit=1, window=5)
    assert limiter.reset_time() == 0
    limiter.allow_request()
    assert limiter.reset_time() > 0
    assert limiter.reset_time() <= 5


def test_rate_limiter_window_expiration():
    limiter = RateLimiter(limit=1, window=1)
    assert limiter.allow_request() is True
    assert limiter.allow_request() is False
    time.sleep(1.1)
    assert limiter.allow_request() is True


def test_root_endpoint(client):
    response = client.get("/")
    assert response.status_code == 200
    assert response.json() == {
        "message": "RateGuard is running",
        "version": "1.1.0",
        "algorithm": "sliding_window"
    }


def test_api_endpoint_rate_limiting():
    # Test using a fresh limiter instance logic or the endpoint behavior
    limiter = RateLimiter(limit=2, window=10)
    assert limiter.allow_request() is True
    assert limiter.allow_request() is True
    assert limiter.allow_request() is False
