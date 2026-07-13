"""Redis token-bucket rate limiter (Section 2).

Chosen pattern: **token bucket** implemented with a single Lua script so the
"read tokens -> refill -> maybe consume -> write back" sequence is atomic.

Why token bucket over the alternatives (expanded in DESIGN.md):
* Fixed window (INCR + EXPIRE) allows a 2x burst across the window boundary
  (200 at 00:59.9 + 200 at 01:00.0). Unacceptable for a hard provider cap.
* Sliding window (sorted set + ZREMRANGEBYSCORE) is accurate but stores one
  member per request -> O(N) memory during a 2,000-request burst.
* Token bucket stores just two numbers (tokens, last-refill-timestamp) per key
  and enforces both a steady rate AND a bounded burst.

Atomicity: everything runs inside one ``EVAL`` (Lua). Redis executes a script
as a single blocking unit, so concurrent Celery workers can never both read the
same token count and double-spend it — no MULTI/EXEC race window.

Failure mode: if Redis is unreachable the limiter **fails closed** (returns
"not allowed") by default, because exceeding a hard third-party cap can get the
whole account throttled/banned. This is configurable via ``fail_open``.
"""

import time

import redis
from django.conf import settings

# KEYS[1] = bucket key
# ARGV[1] = capacity (max tokens / burst)
# ARGV[2] = refill_rate (tokens per second)
# ARGV[3] = now (unix seconds, float)
# ARGV[4] = requested tokens
# ARGV[5] = ttl seconds (safety expiry for idle buckets)
# ARGV[6] = initial tokens for a brand-new bucket (cold-start burst allowance)
# Returns: {allowed(0/1), tokens_remaining, retry_after_seconds}
_TOKEN_BUCKET_LUA = """
local capacity = tonumber(ARGV[1])
local refill_rate = tonumber(ARGV[2])
local now = tonumber(ARGV[3])
local requested = tonumber(ARGV[4])
local ttl = tonumber(ARGV[5])
local initial = tonumber(ARGV[6])

local data = redis.call('HMGET', KEYS[1], 'tokens', 'ts')
local tokens = tonumber(data[1])
local ts = tonumber(data[2])

if tokens == nil then
    tokens = initial
    ts = now
end

-- Refill based on elapsed time since the last update, capped at capacity.
local elapsed = math.max(0, now - ts)
tokens = math.min(capacity, tokens + elapsed * refill_rate)

local allowed = 0
local retry_after = 0
if tokens >= requested then
    tokens = tokens - requested
    allowed = 1
else
    local deficit = requested - tokens
    retry_after = deficit / refill_rate
end

redis.call('HMSET', KEYS[1], 'tokens', tokens, 'ts', now)
redis.call('EXPIRE', KEYS[1], ttl)

return {allowed, tokens, tostring(retry_after)}
"""


class RateLimitResult:
    def __init__(self, allowed, tokens_remaining, retry_after):
        self.allowed = allowed
        self.tokens_remaining = tokens_remaining
        self.retry_after = retry_after

    def __bool__(self):
        return self.allowed


class TokenBucketRateLimiter:
    def __init__(
        self,
        key,
        capacity=None,
        window_seconds=None,
        client=None,
        fail_open=False,
        time_func=time.time,
        initial_tokens=0,
    ):
        self.key = f"ratelimit:{key}"
        self.capacity = capacity if capacity is not None else settings.EMAIL_RATE_LIMIT
        window = (
            window_seconds
            if window_seconds is not None
            else settings.EMAIL_RATE_WINDOW_SECONDS
        )
        # e.g. 200 tokens / 60s => refill 3.33 tokens/sec, steady 200/min.
        self.refill_rate = self.capacity / window
        self.ttl = int(window * 2) + 1
        self.fail_open = fail_open
        # Start empty by default: no cold-start burst, so a fresh bucket cannot
        # exceed the hard provider cap in its first window. Set to `capacity`
        # to allow a full burst on cold start (standard token-bucket behaviour).
        self.initial_tokens = initial_tokens
        # Injectable clock lets tests drive refill deterministically.
        self._time_func = time_func
        self._client = client or redis.Redis.from_url(settings.REDIS_URL)
        self._script = self._client.register_script(_TOKEN_BUCKET_LUA)

    def acquire(self, tokens=1):
        try:
            allowed, remaining, retry_after = self._script(
                keys=[self.key],
                args=[
                    self.capacity,
                    self.refill_rate,
                    self._time_func(),
                    tokens,
                    self.ttl,
                    self.initial_tokens,
                ],
            )
        except redis.RedisError:
            # Redis down: fail closed by default (see module docstring).
            if self.fail_open:
                return RateLimitResult(True, 0, 0.0)
            return RateLimitResult(False, 0, float(settings.EMAIL_RATE_WINDOW_SECONDS))
        return RateLimitResult(bool(allowed), float(remaining), float(retry_after))

    def reset(self):
        self._client.delete(self.key)
