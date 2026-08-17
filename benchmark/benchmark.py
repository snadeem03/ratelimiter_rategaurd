import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.algorithms.fixed_window import FixedWindowRateLimiter
from app.algorithms.sliding_window import SlidingWindowRateLimiter
from app.algorithms.token_bucket import TokenBucketRateLimiter
from app.algorithms.leaky_bucket import LeakyBucketRateLimiter


def run_benchmark(name, limiter, requests):
    start_time = time.perf_counter()

    allowed = 0
    blocked = 0

    for _ in range(requests):

        if limiter.allow_request():
            allowed += 1
        else:
            blocked += 1

    end_time = time.perf_counter()

    execution_time = end_time - start_time

    return {
        "algorithm": name,
        "requests": requests,
        "allowed": allowed,
        "blocked": blocked,
        "execution_time": execution_time
    }


def main():

    requests = 1000

    limiters = [
        (
            "Fixed Window",
            FixedWindowRateLimiter(
                limit=100,
                window=60
            )
        ),

        (
            "Sliding Window",
            SlidingWindowRateLimiter(
                limit=100,
                window=60
            )
        ),

        (
            "Token Bucket",
            TokenBucketRateLimiter(
                capacity=100,
                refill_rate=100 / 60
            )
        ),

        (
            "Leaky Bucket",
            LeakyBucketRateLimiter(
                capacity=100,
                leak_rate=100 / 60
            )
        )
    ]

    print("\nRateGuard Benchmark")
    print("=" * 60)

    for name, limiter in limiters:

        result = run_benchmark(
            name,
            limiter,
            requests
        )

        print(f"\nAlgorithm: {result['algorithm']}")
        print(f"Requests: {result['requests']}")
        print(f"Allowed: {result['allowed']}")
        print(f"Blocked: {result['blocked']}")
        print(
            f"Execution Time: "
            f"{result['execution_time']:.6f}s"
        )


if __name__ == "__main__":
    main()