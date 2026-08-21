"""Startup-time validation of the global rate-limit environment.

``app.main`` must refuse to boot with a non-integer or non-positive
``RATE_LIMIT``/``RATE_LIMIT_WINDOW`` instead of crashing later on the
first request (a ``window=0`` previously produced ZeroDivisionError in
the token/leaky bucket factories at request time).

The tests run ``import app.main`` in a subprocess because the validation
executes at module import; the in-process module is already imported by
other tests with valid defaults.
"""

import os
import subprocess
import sys


def _import_main_with_env(extra_env: dict) -> subprocess.CompletedProcess:
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("RATE_LIMIT")
    }

    # Keep this test independent from any Redis configuration.
    env.pop("REDIS_URL", None)

    env.update(extra_env)

    return subprocess.run(
        [sys.executable, "-c", "import app.main"],
        capture_output=True,
        text=True,
        env=env,
        cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    )


class TestGlobalLimitValidation:
    def test_valid_defaults_import_cleanly(self):
        result = _import_main_with_env({})

        assert result.returncode == 0, result.stderr

    def test_zero_rate_limit_fails_fast(self):
        result = _import_main_with_env({"RATE_LIMIT": "0"})

        assert result.returncode != 0
        assert "RATE_LIMIT" in result.stderr

    def test_negative_window_fails_fast(self):
        result = _import_main_with_env({"RATE_LIMIT_WINDOW": "-10"})

        assert result.returncode != 0
        assert "RATE_LIMIT_WINDOW" in result.stderr

    def test_non_integer_limit_fails_fast(self):
        result = _import_main_with_env({"RATE_LIMIT": "five"})

        assert result.returncode != 0
        assert "RATE_LIMIT" in result.stderr

    def test_non_integer_window_names_the_variable(self):
        result = _import_main_with_env({"RATE_LIMIT_WINDOW": "soon"})

        assert result.returncode != 0
        assert "RATE_LIMIT_WINDOW must be an integer" in result.stderr

    def test_invalid_route_limits_fail_at_startup(self):
        result = _import_main_with_env(
            {"RATE_LIMIT_ROUTES": "/api/x:0:60"}
        )

        assert result.returncode != 0
        assert "RATE_LIMIT_ROUTES" in result.stderr
