import time
import uuid

import redis
from django.conf import settings


# Sliding-window log in a single Lua script so the trim/count/insert cycle is
# atomic - Redis runs a script as one blocking unit, so two workers can't both
# see the same count and double-spend a slot.
#
# We keep one timestamped entry per allowed send in a sorted set and, on each
# call, drop everything older than the window and count what's left. Unlike a
# token bucket this bounds *any* rolling window to the limit exactly (a token
# bucket of capacity C and rate r allows up to C + r*window in a window), which
# is what a hard "N per minute" provider cap actually needs.
#
# KEYS[1] zset key
# ARGV: now, window, limit, member, ttl
# returns: {allowed, remaining, retry_after}
_SLIDING_WINDOW_LUA = """
local now = tonumber(ARGV[1])
local window = tonumber(ARGV[2])
local limit = tonumber(ARGV[3])
local member = ARGV[4]
local ttl = tonumber(ARGV[5])

redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', now - window)
local count = redis.call('ZCARD', KEYS[1])

local allowed = 0
local retry_after = 0
if count < limit then
    redis.call('ZADD', KEYS[1], now, member)
    count = count + 1
    allowed = 1
else
    local oldest = redis.call('ZRANGE', KEYS[1], 0, 0, 'WITHSCORES')
    if oldest[2] then
        retry_after = tonumber(oldest[2]) + window - now
        if retry_after < 0 then retry_after = 0 end
    end
end

redis.call('EXPIRE', KEYS[1], ttl)

local remaining = limit - count
if remaining < 0 then remaining = 0 end
return {allowed, remaining, tostring(retry_after)}
"""


class RateLimitResult:
    def __init__(self, allowed, remaining, retry_after):
        self.allowed = allowed
        self.remaining = remaining
        self.retry_after = retry_after

    def __bool__(self):
        return self.allowed


class SlidingWindowRateLimiter:
    def __init__(
        self,
        key,
        limit=None,
        window_seconds=None,
        client=None,
        fail_open=False,
        time_func=time.time,
    ):
        self.key = f"ratelimit:{key}"
        self.limit = limit if limit is not None else settings.EMAIL_RATE_LIMIT
        self.window = (
            window_seconds
            if window_seconds is not None
            else settings.EMAIL_RATE_WINDOW_SECONDS
        )
        self.ttl = int(self.window * 2) + 1
        self.fail_open = fail_open
        self._time_func = time_func
        self._client = client or redis.Redis.from_url(settings.REDIS_URL)
        self._script = self._client.register_script(_SLIDING_WINDOW_LUA)

    def acquire(self):
        now = self._time_func()
        member = f"{now:.6f}-{uuid.uuid4().hex}"
        try:
            allowed, remaining, retry_after = self._script(
                keys=[self.key],
                args=[now, self.window, self.limit, member, self.ttl],
            )
        except redis.RedisError:
            # Redis down: don't send unless the caller opted into fail-open.
            # Blowing past a hard provider cap is worse than pausing.
            if self.fail_open:
                return RateLimitResult(True, 0, 0.0)
            return RateLimitResult(False, 0, float(self.window))
        return RateLimitResult(bool(allowed), int(remaining), float(retry_after))

    def reset(self):
        self._client.delete(self.key)
