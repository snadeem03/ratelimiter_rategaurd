from abc import ABC, abstractmethod

class RateLimiter(ABC):
    @abstractmethod
    def allow_request(self) -> bool:
        pass

    @abstractmethod
    def remaining_requests(self) -> int:
        pass

    @abstractmethod
    def reset_time(self) -> int:
        pass

class FixedWindowRateLimiter(RateLimiter):
    pass

class SlidingWindowRateLimiter(RateLimiter):
    pass

class TokenBucketRateLimiter(RateLimiter):
    pass

class LeakyBucketRateLimiter(RateLimiter):
    pass

