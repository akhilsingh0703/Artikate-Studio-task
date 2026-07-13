import random

from celery import shared_task
from celery.utils.log import get_task_logger

from .models import DeadLetter, EmailJob
from .provider import EmailProviderError, PermanentEmailError, send_email
from .rate_limiter import SlidingWindowRateLimiter

logger = get_task_logger(__name__)

MAX_RETRIES = 5
# The task allows unlimited retries (see the decorator) because being throttled
# is flow control, not a failure - a finite cap silently drops jobs once a
# backlog is rate-limited more times than the cap. Real (provider) failures are
# still bounded to MAX_RETRIES by the explicit check in process_email_job, so
# they dead-letter instead of retrying forever.
RATE_RETRY_JITTER_SECONDS = 1.0

# outcomes returned by process_email_job
SENT = "sent"
DEAD = "dead"
SKIP = "skip"
RETRY_RATE = "retry_rate"
RETRY_ERROR = "retry_error"


def email_rate_limiter():
    # one global window shared by all workers = the provider-wide cap
    return SlidingWindowRateLimiter(key="email:global")


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


# max_retries=None -> unlimited. self.retry(max_retries=None) does NOT mean
# unlimited (Celery reads None there as "use the task default"), so it has to be
# set here. The error path stays bounded by process_email_job's retries check.
@shared_task(bind=True, max_retries=None)
def send_email_task(self, job_id, limiter=None):
    limiter = limiter or email_rate_limiter()
    outcome, info = process_email_job(job_id, limiter, self.request.retries)
    if outcome == RETRY_RATE:
        countdown = info + random.uniform(0, RATE_RETRY_JITTER_SECONDS)
        raise self.retry(countdown=countdown)
    if outcome == RETRY_ERROR:
        raise self.retry(countdown=info)
    return outcome
