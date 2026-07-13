# DESIGN.md — Section 2: rate-limited async job queue

## Problem

We send transactional emails through a provider capped at 200 emails/minute,
while the platform itself can get bursts of ~2,000 requests in about 10 seconds.
So the queue has to respect the provider's rate limit, retry transient failures,
and never drop a job if a worker dies mid-run.

## Why Celery + Redis

We already run Redis for the rate limiter, so putting Celery on Redis doesn't
add any new infrastructure. Celery also gives us the two things that are
annoying to get right by hand: crash-safe redelivery and mature retry/backoff.

I looked at a couple of alternatives:

- Django-Q / Django-RQ — simpler, but the crash-safety story is weaker and the
  retry/backoff handling is less mature.
- A custom DB table with a cron poller — full control, no broker, but you end up
  reinventing visibility timeouts, acking, retries and concurrency, which is
  exactly what Celery already does.

Celery + Redis won. The three settings that actually buy the crash-safety:

```python
CELERY_TASK_ACKS_LATE = True              # ack only after the task returns
CELERY_TASK_REJECT_ON_WORKER_LOST = True  # lost worker -> requeue, don't ack
CELERY_WORKER_PREFETCH_MULTIPLIER = 1     # don't hoard messages a crash would drop
```

## The rate limiter: a Redis token bucket

It's in `emails/rate_limiter.py`, implemented as a token bucket inside a single
Lua script (`EVAL`).

Why token bucket and not the other two:

- Fixed window (INCR + EXPIRE) lets a client send a full `capacity` at 00:59.9
  and another full `capacity` at 01:00.0, i.e. double the limit across the
  boundary. That's a non-starter against a hard provider cap.
- Sliding window (a sorted set + ZREMRANGEBYSCORE) is accurate, but it stores
  one member per request. During a 2,000-request burst that's 2,000 members per
  key, and both memory and the O(log N) insert cost grow with traffic.
- Token bucket stores just two numbers per key (`tokens` and `ts`). It enforces
  a steady refill rate *and* a bounded burst, and it can hand back a precise
  `retry_after` so callers reschedule instead of spinning.

### Atomicity

The whole "read tokens, refill by elapsed time, maybe consume, write back"
sequence runs in one Lua script. Redis runs a script as a single atomic,
blocking unit, so two workers can't both read the same count and double-spend
it. That's stronger than a MULTI/EXEC transaction (which can't branch on a value
it read mid-transaction) and simpler than WATCH-based optimistic retries.

### The cold-start trade-off

`initial_tokens` defaults to 0 for the email bucket. If the bucket started full
it would allow an immediate burst of `capacity` on top of the refill — up to 2x
capacity in the first window. Starting empty turns the limiter into a pure
smoother: under the flash-sale's sustained pressure it emits at exactly the
refill rate (200/min), which is what the burst test checks. If a cold-start
burst is fine for some bucket, pass `initial_tokens=capacity`.

### Failure mode: fail closed

If Redis is unreachable, `acquire()` returns "not allowed" by default. Blowing
past a hard third-party cap can get the whole account throttled or banned, so
not sending is the safer default. Buckets that don't care can pass
`fail_open=True`.

## Retry and dead-letter

`emails/tasks.py` keeps the decision logic in a plain `process_email_job()`
(testable without a broker) and wraps it in a thin Celery task:

- Rate-limited -> `self.retry(countdown=retry_after)` with a high ceiling
  (`RATE_LIMIT_MAX_RETRIES`), since being throttled is expected, not a failure.
  No `time.sleep()` — the job goes back on the broker.
- Transient provider error (`EmailProviderError`) -> `self.retry` with
  exponential backoff and full jitter (`2 ** attempt`, capped at 60s), up to
  `MAX_RETRIES = 5`.
- Retries exhausted, or a permanent error (`PermanentEmailError`) -> the job is
  dead-lettered: the `EmailJob` row is marked `DEAD` and a `DeadLetter` row is
  written so it can be inspected or replayed.

## Idempotency

Because `acks_late` gives at-least-once delivery, a job can be redelivered after
a crash that happened after the send but before the ack. `process_email_job`
returns early when `job.status == SENT`, so a redelivered job is never emailed
twice.

## Testing

`emails/tests.py` runs the real task logic against the real Redis limiter
through an in-process `InlineBroker` with a fake clock — it mirrors Celery's
reschedule-on-retry without actually sleeping. The 500-job test asserts:

1. Nothing is lost — all 500 reach `SENT`.
2. The rate is never exceeded — no 60-second window contains more than 200 sends.
3. Retries work — an intentional transient failure retries and eventually
   succeeds (`attempts >= 2`, final status `SENT`).

The SIGKILL walk-through is in `ANSWERS.md` §2.
