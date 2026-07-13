import time

import redis
from django.conf import settings


# Token bucket in a single Lua script so the whole read/refill/consume/write
# cycle is atomic. Redis runs a script as one blocking unit, so two workers
# can't both read the same count and double-spend.
#
# KEYS[1] bucket key
# ARGV: capacity, refill_rate (tokens/sec), now, requested, ttl, initial_tokens
# returns: {allowed, tokens_left, retry_after}
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

local elapsed = math.max(0, now - ts)
tokens = math.min(capacity, tokens + elapsed * refill_rate)

local allowed = 0
local retry_after = 0
if tokens >= requested then
    tokens = tokens - requested
    allowed = 1
else
    retry_after = (requested - tokens) / refill_rate
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
        # 200 tokens / 60s -> 3.33/sec steady rate.
        self.refill_rate = self.capacity / window
        self.ttl = int(window * 2) + 1
        self.fail_open = fail_open
        # Default to an empty bucket so a fresh key can't burst past the cap in
        # its first window. Pass capacity if a cold-start burst is fine.
        self.initial_tokens = initial_tokens
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
            # Redis down: don't send unless the caller opted into fail-open.
            # Blowing past a hard provider cap is worse than pausing.
            if self.fail_open:
                return RateLimitResult(True, 0, 0.0)
            return RateLimitResult(False, 0, float(settings.EMAIL_RATE_WINDOW_SECONDS))
        return RateLimitResult(bool(allowed), float(remaining), float(retry_after))

    def reset(self):
        self._client.delete(self.key)
