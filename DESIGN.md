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

## The rate limiter: a Redis sliding-window log

It's in `emails/rate_limiter.py`: a sorted set of timestamps per key, driven by
a single Lua script (`EVAL`). Each allowed send drops a `now`-scored member in
the set; every call first trims everything older than the window and counts
what's left.

Why this and not the other two:

- Fixed window (INCR + EXPIRE) lets a client send a full limit at 00:59.9 and
  another full limit at 01:00.0 — 2x the cap across the boundary. Non-starter
  against a hard provider cap.
- A **token bucket** is the tempting answer (it stores only two numbers), but it
  does *not* give a hard "N per rolling minute". A bucket of capacity `C` and
  rate `r` allows up to `C + r*window` in some window. With `C = 200` and
  `r = 200/60` that's up to ~400 in a 60s window, and under real concurrent
  workers I measured a 60s window hit **219** — over the provider cap. Shrinking
  `C` to fix that just turns the bucket into a worse approximation of a sliding
  window.
- The sliding-window log is exactly the "≤ N per rolling window" guarantee the
  brief asks for. At every allowed send the trailing-window count is `≤ limit`,
  which means *any* window of `window` length holds `≤ limit` sends.

The usual knock on the sliding-window log is memory — one member per request.
Here it's bounded: excess acquires are *denied*, not stored, so a key only ever
holds up to `limit` (~200) live timestamps regardless of how big the burst is.
A 2,000-request spike queues 2,000 jobs on the broker but the limiter set stays
at ~200. Old members expire via the window trim plus a TTL.

### Atomicity

The whole "trim old, count, maybe insert, write" sequence runs in one Lua
script. Redis runs a script as a single atomic, blocking unit, so two workers
can't both see the same count and double-spend a slot. That's stronger than a
MULTI/EXEC transaction (which can't branch on a value it read mid-transaction)
and simpler than WATCH-based optimistic retries.

### Failure mode: fail closed

If Redis is unreachable, `acquire()` returns "not allowed" by default. Blowing
past a hard third-party cap can get the whole account throttled or banned, so
not sending is the safer default. Callers that don't care can pass
`fail_open=True`.

## Retry and dead-letter

`emails/tasks.py` keeps the decision logic in a plain `process_email_job()`
(testable without a broker) and wraps it in a thin Celery task:

- Rate-limited -> `self.retry(countdown=retry_after + jitter)`. The task is
  declared `max_retries=None` (unlimited) because being throttled is flow
  control, not a failure. A finite cap here is a job-loss bug: under a sustained
  backlog a job can be rate-limited more times than the cap and then get
  dropped. (Watch out: `self.retry(max_retries=None)` does *not* mean unlimited
  — Celery reads `None` there as "use the task default" — so it has to be set on
  the task.) The jitter spreads a few hundred rescheduled jobs out in time so
  they don't all wake up and re-collide at once (a thundering herd). No
  `time.sleep()` — the job goes back on the broker.
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

The `InlineBroker` reschedules rate-limited jobs indefinitely, which is what the
real system does — but that means it *can't* catch a finite retry cap in the
actual Celery wrapper. `RateLimitRetryPolicyTests` covers that gap directly
(the task must be `max_retries=None`, and a rate-limited job must reschedule
rather than drop). This split matters: an earlier version passed the 500-job
test but lost hundreds of jobs live, because the cap only bit under a real
worker.

The SIGKILL walk-through is in `ANSWERS.md` §2.
