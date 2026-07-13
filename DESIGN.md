# DESIGN.md — Section 2: Rate-Limited Async Job Queue

## Problem

Send transactional emails through a provider capped at **200 emails/minute**,
while the platform receives bursts of **2,000 requests in ~10 seconds**. The
queue must:

1. respect the rate limit,
2. retry on transient failure,
3. never lose a job if the worker crashes mid-run.

## Architecture choice: Celery + Redis

| Option | Pros | Cons | Verdict |
| --- | --- | --- | --- |
| **Celery + Redis** | Battle-tested; `acks_late` + `reject_on_worker_lost` give redelivery on crash; built-in retry/backoff; Redis also hosts the rate limiter; horizontal scaling of workers | Extra moving part (broker); at-least-once delivery means tasks must be idempotent | **Chosen** |
| **Django Q / Django-RQ** | Simpler, fewer concepts; DB or Redis broker | Weaker crash-safety story; smaller ecosystem; retry/backoff less mature | Rejected |
| **Custom (DB table + cron/poller)** | Full control; no broker | We would reinvent visibility timeouts, retries, acking, and concurrency — exactly the things Celery already gets right | Rejected |

The task already runs Redis for the rate limiter, so Celery-on-Redis adds no new
infrastructure. The two settings that buy crash-safety are:

```python
CELERY_TASK_ACKS_LATE = True            # ack only AFTER the task returns
CELERY_TASK_REJECT_ON_WORKER_LOST = True  # lost worker -> requeue, don't ack
CELERY_WORKER_PREFETCH_MULTIPLIER = 1   # don't hoard messages a crash would drop
```

## Rate limiter: Redis token bucket (Option A)

Implemented in `emails/rate_limiter.py` as a **token bucket** inside a single
**Lua script** (`EVAL`).

### Why token bucket over the alternatives

- **Fixed window (INCR + EXPIRE)** — a client can send `capacity` at `00:59.9`
  and another `capacity` at `01:00.0`, i.e. **2× the limit** across the boundary.
  Unacceptable against a hard provider cap.
- **Sliding window (sorted set + ZREMRANGEBYSCORE)** — accurate, but stores one
  ZSET member *per request*. During a 2,000-request burst that is 2,000 members
  per key; memory and `O(log N)` insertion cost scale with traffic.
- **Token bucket** — stores just two numbers per key (`tokens`, `ts`). It
  enforces a **steady refill rate** *and* a **bounded burst** (`capacity`), and
  reports a precise `retry_after` so callers can reschedule instead of spin.

### Atomicity

The whole "read tokens → refill by elapsed time → maybe consume → write back"
sequence runs inside **one Lua script**. Redis executes a script as a single
atomic, blocking unit, so two Celery workers can never both read the same token
count and double-spend it. This is stronger than a `MULTI/EXEC` transaction
(which cannot branch on a value read mid-transaction) and simpler than
`WATCH`-based optimistic retries.

### Cold-start / burst trade-off

`initial_tokens` defaults to **0** for the email bucket. A bucket that starts
*full* would allow an immediate burst of `capacity` on top of the refill — up to
`2 × capacity` in the first window. Starting empty makes the limiter a pure
**smoother**: under the flash-sale's continuous pressure it emits at exactly the
refill rate (200/min), which is what the burst test asserts. Set
`initial_tokens=capacity` if a cold-start burst is acceptable for a given bucket.

### Failure mode: fail **closed**

If Redis is unreachable, `acquire()` returns *not allowed* by default. Exceeding
a hard third-party cap can get the whole account throttled or banned, so we
prefer to *not send* over *risk a ban*. `fail_open=True` flips this for
non-critical buckets.

## Retry & dead-letter

`emails/tasks.py` splits logic into a pure `process_email_job()` (testable
without a broker) and a thin Celery wrapper:

- **Rate-limited** → `self.retry(countdown=retry_after)` with a high ceiling
  (`RATE_LIMIT_MAX_RETRIES`); throttling is expected, not a failure. No
  `time.sleep()` — the job goes back on the broker.
- **Transient provider error** (`EmailProviderError`) → `self.retry` with
  **exponential backoff + full jitter** (`2 ** attempt`, capped at 60s), up to
  `MAX_RETRIES=5`.
- **Retries exhausted** or **permanent error** (`PermanentEmailError`) →
  **dead-lettered**: the `EmailJob` row is marked `DEAD` and a `DeadLetter`
  record is written for inspection/replay.

## Idempotency

Because `acks_late` gives *at-least-once* delivery, a job can be redelivered
after a crash that occurred *after* the send but *before* the ack.
`process_email_job` short-circuits when `job.status == SENT`, so a redelivered
job is never emailed twice.

## Testing

`emails/tests.py` drives the **real** task logic and the **real** Redis limiter
through an in-process `InlineBroker` with a fake clock (mirroring Celery's
reschedule-on-retry without real sleeping). The 500-job test asserts:

1. **No job lost** — all 500 reach a terminal state (`SENT`).
2. **Rate never exceeded** — no 60-second sliding window contains > 200 sends.
3. **Retry works** — an intentional transient failure is retried and succeeds
   (`attempts >= 2`, final status `SENT`).

See `ANSWERS.md` (Section 2) for the SIGKILL walk-through.
