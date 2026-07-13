import random

from celery import shared_task
from celery.utils.log import get_task_logger

from .models import DeadLetter, EmailJob
from .provider import EmailProviderError, PermanentEmailError, send_email
from .rate_limiter import TokenBucketRateLimiter

logger = get_task_logger(__name__)

MAX_RETRIES = 5
RATE_LIMIT_MAX_RETRIES = 50  # being throttled is normal under a burst

# outcomes returned by process_email_job
SENT = "sent"
DEAD = "dead"
SKIP = "skip"
RETRY_RATE = "retry_rate"
RETRY_ERROR = "retry_error"


def email_rate_limiter():
    # one global bucket shared by all workers = the provider-wide cap
    return TokenBucketRateLimiter(key="email:global")


def backoff_seconds(attempt):
    base = min(2 ** attempt, 60)
    return base / 2 + random.uniform(0, base / 2)  # full jitter


def _dead_letter(job, error):
    job.status = EmailJob.Status.DEAD
    job.last_error = str(error)
    job.save(update_fields=["status", "last_error", "attempts", "updated_at"])
    DeadLetter.objects.get_or_create(email_job=job, defaults={"error": str(error)})
    logger.error("dead-lettered EmailJob %s: %s", job.pk, error)


def process_email_job(job_id, limiter, retries):
    """Decide what to do with one job and do the DB writes.

    Returns (outcome, info). Rescheduling is left to the caller so this can be
    unit-tested without a running worker.
    """
    try:
        job = EmailJob.objects.get(pk=job_id)
    except EmailJob.DoesNotExist:
        return SKIP, "missing"

    # acks_late means a job can be redelivered after a crash that happened
    # between send and ack, so don't send it twice.
    if job.status == EmailJob.Status.SENT:
        return SKIP, "already-sent"

    result = limiter.acquire()
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
