"""Celery tasks for the rate-limited email queue (Section 2).

Design summary (full reasoning in DESIGN.md):

* The decision logic lives in :func:`process_email_job`, a plain function that
  returns an *outcome* and takes no Celery internals. The Celery task is a thin
  wrapper that translates outcomes into ``self.retry`` calls. This keeps the
  hard-to-test parts (rate limiting, backoff, dead-lettering) unit-testable
  without a live broker.
* Rate limiting: before sending, acquire a token from the Redis token bucket.
  If none is available the job is retried after ``retry_after`` — it goes back
  on the broker, never blocking the worker with ``time.sleep()``.
* Transient failures retry with **exponential backoff + jitter**, capped at
  ``MAX_RETRIES``; then they are dead-lettered.
* Permanent failures are dead-lettered immediately.
* Crash safety comes from ``task_acks_late=True`` + ``reject_on_worker_lost``
  (settings): a SIGKILL'd worker's un-acked job is redelivered.
"""

import random

from celery import shared_task
from celery.utils.log import get_task_logger

from .models import DeadLetter, EmailJob
from .provider import EmailProviderError, PermanentEmailError, send_email
from .rate_limiter import TokenBucketRateLimiter

logger = get_task_logger(__name__)

MAX_RETRIES = 5
RATE_LIMIT_MAX_RETRIES = 50  # throttling is expected under burst; retry a lot

# Outcome codes returned by process_email_job.
SENT = "sent"
DEAD = "dead"
SKIP = "skip"
RETRY_RATE = "retry_rate"    # no token available; reschedule, don't count as failure
RETRY_ERROR = "retry_error"  # transient provider error; backoff retry


def email_rate_limiter():
    # Global bucket shared by all workers -> enforces the provider-wide cap.
    return TokenBucketRateLimiter(key="email:global")


def backoff_seconds(attempt):
    # 2 ** attempt with full jitter, capped at 60s. attempt is 0-based.
    base = min(2 ** attempt, 60)
    return base / 2 + random.uniform(0, base / 2)


def _dead_letter(job, error):
    job.status = EmailJob.Status.DEAD
    job.last_error = str(error)
    job.save(update_fields=["status", "attempts", "last_error", "updated_at"])
    DeadLetter.objects.get_or_create(email_job=job, defaults={"error": str(error)})
    logger.error("EmailJob %s dead-lettered: %s", job.pk, error)


def process_email_job(job_id, limiter, retries):
    """Pure decision function for a single job.

    Returns ``(outcome, info)`` where ``info`` is a countdown (for retries),
    an error, or ``None``. Performs the DB writes/side effects but leaves the
    actual rescheduling to the caller (the Celery task or a test harness).
    """
    try:
        job = EmailJob.objects.get(pk=job_id)
    except EmailJob.DoesNotExist:
        return SKIP, "missing"

    if job.status == EmailJob.Status.SENT:
        # Idempotency: acks_late means a job can be redelivered after a crash
        # that happened *after* send but *before* ack. Don't send twice.
        return SKIP, "already-sent"

    result = limiter.acquire(tokens=1)
    if not result.allowed:
        return RETRY_RATE, max(result.retry_after, 0.05)

    job.attempts += 1
    try:
        send_email(job.recipient, job.kind, job.payload)
    except PermanentEmailError as exc:
        _dead_letter(job, exc)
        return DEAD, exc
    except EmailProviderError as exc:
        if retries >= MAX_RETRIES:
            _dead_letter(job, exc)
            return DEAD, exc
        job.status = EmailJob.Status.FAILED
        job.last_error = str(exc)
        job.save(update_fields=["status", "attempts", "last_error", "updated_at"])
        return RETRY_ERROR, backoff_seconds(retries)

    job.status = EmailJob.Status.SENT
    job.last_error = ""
    job.save(update_fields=["status", "attempts", "last_error", "updated_at"])
    return SENT, None


@shared_task(bind=True, max_retries=MAX_RETRIES)
def send_email_task(self, job_id, limiter=None):
    limiter = limiter or email_rate_limiter()
    outcome, info = process_email_job(job_id, limiter, self.request.retries)
    if outcome == RETRY_RATE:
        raise self.retry(countdown=info, max_retries=RATE_LIMIT_MAX_RETRIES)
    if outcome == RETRY_ERROR:
        raise self.retry(countdown=info)
    return outcome
