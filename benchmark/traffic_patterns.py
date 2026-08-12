import time


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