import threading
import time

TRAFFIC_FNS = {
    "normal": "normal_traffic",
    "burst": "burst_traffic",
    "sustained": "sustained_traffic",
}


def normal_traffic(limiter, requests, interval):
    results = []

    for _ in range(requests):

        allowed = limiter.allow_request()

        results.append(allowed)

        time.sleep(interval)

    return results


def burst_traffic(limiter, requests):
    results = []

    for _ in range(requests):

        allowed = limiter.allow_request()

        results.append(allowed)

    return results


def sustained_traffic(
    limiter,
    requests,
    interval
):
    results = []

    for _ in range(requests):

        allowed = limiter.allow_request()

        results.append(allowed)

        time.sleep(interval)

    return results


class TimedLimiter:
    """Wrap a rate limiter to record per-request latency and counters.

    Keeps the existing traffic functions untouched: they call
    ``allow_request()`` on this wrapper and the wrapper measures the real
    limiter underneath.
    """

    def __init__(self, limiter):
        self.limiter = limiter
        self.latencies_ms = []
        self.allowed = 0
        self.rejected = 0
        self._lock = threading.Lock()

    def allow_request(self):
        start = time.perf_counter()
        ok = bool(self.limiter.allow_request())
        latency_ms = (time.perf_counter() - start) * 1000

        with self._lock:
            self.latencies_ms.append(latency_ms)
            if ok:
                self.allowed += 1
            else:
                self.rejected += 1

        return ok


def concurrent_traffic(make_limiter, traffic, requests, concurrency, interval=None):
    """Run a traffic pattern across ``concurrency`` threads.

    ``make_limiter(thread_index)`` returns the limiter for each thread
    (for the memory backend all threads share one instance; for Redis
    each thread gets its own instance sharing the same keys).

    Returns a list of ``(allowed, rejected, latencies_ms)`` tuples, one
    per thread, so the caller can merge results.
    """
    if traffic not in TRAFFIC_FNS:
        raise KeyError(f"Unknown traffic pattern: {traffic}")

    if concurrency < 1:
        raise ValueError("concurrency must be >= 1")

    per_thread, remainder = divmod(requests, concurrency)

    counts = [
        per_thread + (1 if i < remainder else 0)
        for i in range(concurrency)
    ]

    summaries = [None] * concurrency

    def worker(index):
        timed = TimedLimiter(make_limiter(index))
        count = counts[index]

        if traffic == "burst":
            burst_traffic(timed, count)
        elif traffic == "normal":
            normal_traffic(timed, count, interval or 0.0)
        else:
            sustained_traffic(timed, count, interval or 0.0)

        summaries[index] = (
            timed.allowed,
            timed.rejected,
            timed.latencies_ms,
        )

    threads = [
        threading.Thread(target=worker, args=(index,))
        for index in range(concurrency)
    ]

    for thread in threads:
        thread.start()

    for thread in threads:
        thread.join()

    return summaries