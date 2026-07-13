"""Section 2 tests.

Two levels:
* ``TokenBucketRateLimiterTests`` — unit tests for the Redis limiter, driven by
  a fake clock so refill is deterministic. Requires a running Redis.
* ``QueueBurstTests`` — the required 500-job burst test. It drives the *real*
  task logic (``process_email_job``) and the *real* Redis limiter through an
  in-process broker (``InlineBroker``) that mimics Celery's reschedule-on-retry
  behaviour with a fake clock. This lets us assert the three required
  properties deterministically without a live worker.
"""

import heapq
import unittest

import redis as redis_lib
from django.conf import settings
from django.test import TestCase

from . import tasks
from .models import DeadLetter, EmailJob
from .provider import EmailProviderError
from .rate_limiter import TokenBucketRateLimiter


def _redis_available():
    try:
        redis_lib.Redis.from_url(settings.REDIS_URL).ping()
        return True
    except redis_lib.RedisError:
        return False


REDIS_UP = _redis_available()


class FakeClock:
    def __init__(self, start=1_000_000.0):
        self.t = start

    def time(self):
        return self.t

    def advance(self, dt):
        self.t += dt


@unittest.skipUnless(REDIS_UP, "Redis not available")
class TokenBucketRateLimiterTests(TestCase):
    def _limiter(self, clock, capacity=10, window=10):
        lim = TokenBucketRateLimiter(
            key="test:unit",
            capacity=capacity,
            window_seconds=window,
            time_func=clock.time,
            # These tests exercise the classic burst-allowed bucket, so start full.
            initial_tokens=capacity,
        )
        lim.reset()
        return lim

    def test_allows_up_to_capacity_then_blocks(self):
        clock = FakeClock()
        lim = self._limiter(clock, capacity=10, window=10)
        allowed = sum(1 for _ in range(15) if lim.acquire())
        # At a single instant only `capacity` tokens exist.
        self.assertEqual(allowed, 10)

    def test_refills_over_time(self):
        clock = FakeClock()
        lim = self._limiter(clock, capacity=10, window=10)  # 1 token/sec
        for _ in range(10):
            lim.acquire()
        self.assertFalse(lim.acquire())  # bucket empty
        clock.advance(3)                 # 3 seconds -> ~3 tokens back
        allowed = sum(1 for _ in range(5) if lim.acquire())
        self.assertEqual(allowed, 3)

    def test_retry_after_is_reported_when_blocked(self):
        clock = FakeClock()
        lim = self._limiter(clock, capacity=1, window=10)  # 0.1 token/sec
        self.assertTrue(lim.acquire())
        result = lim.acquire()
        self.assertFalse(result.allowed)
        # Need 1 token at 0.1/sec => ~10s.
        self.assertAlmostEqual(result.retry_after, 10, delta=1)

    def test_fails_closed_when_redis_down(self):
        lim = TokenBucketRateLimiter(
            key="test:down",
            capacity=10,
            window_seconds=10,
            client=redis_lib.Redis.from_url("redis://127.0.0.1:6390/0"),
            fail_open=False,
        )
        self.assertFalse(lim.acquire().allowed)


class InlineBroker:
    """Minimal broker that drives real task logic with a fake clock.

    Holds ``(eta, seq, job_id, retries)`` in a heap. On a retry outcome the job
    is re-enqueued at ``clock + countdown`` — exactly what Celery does with
    ``self.retry(countdown=...)``, but without real sleeping. Records the fake
    timestamp of each successful send so we can assert the rate limit.
    """

    def __init__(self, limiter, clock):
        self.limiter = limiter
        self.clock = clock
        self._heap = []
        self._seq = 0
        self.send_times = []

    def enqueue(self, job_id, retries=0, eta=None):
        eta = self.clock.time() if eta is None else eta
        heapq.heappush(self._heap, (eta, self._seq, job_id, retries))
        self._seq += 1

    def run(self):
        while self._heap:
            eta, _, job_id, retries = heapq.heappop(self._heap)
            if self.clock.time() < eta:
                self.clock.t = eta  # jump forward to the next scheduled job
            before = EmailJob.objects.get(pk=job_id).status
            outcome, info = tasks.process_email_job(job_id, self.limiter, retries)
            if outcome == tasks.SENT and before != EmailJob.Status.SENT:
                self.send_times.append(self.clock.time())
                self.clock.advance(0.001)  # each real send takes a little time
            elif outcome == tasks.RETRY_RATE:
                self.enqueue(job_id, retries, eta=self.clock.time() + info)
            elif outcome == tasks.RETRY_ERROR:
                self.enqueue(job_id, retries + 1, eta=self.clock.time() + info)


@unittest.skipUnless(REDIS_UP, "Redis not available")
class QueueBurstTests(TestCase):
    def _limiter(self, clock, capacity=200, window=60, initial_tokens=0):
        lim = TokenBucketRateLimiter(
            key="test:burst",
            capacity=capacity,
            window_seconds=window,
            time_func=clock.time,
            initial_tokens=initial_tokens,
        )
        lim.reset()
        return lim

    def test_500_jobs_no_loss_rate_respected_and_retry(self):
        clock = FakeClock()
        capacity, window = 200, 60
        limiter = self._limiter(clock, capacity, window)

        # 500 jobs; job index 42 will fail transiently exactly once, then succeed.
        flaky_recipient = "user42@artikate.test"
        jobs = EmailJob.objects.bulk_create(
            [
                EmailJob(recipient=f"user{i}@artikate.test",
                         kind=EmailJob.Kind.ORDER_CONFIRMATION)
                for i in range(500)
            ]
        )

        # Patch the provider to fail once for the flaky recipient.
        state = {"failed_once": False}
        real_send = tasks.send_email

        def flaky_send(recipient, kind, payload):
            if recipient == flaky_recipient and not state["failed_once"]:
                state["failed_once"] = True
                raise EmailProviderError("temporary 503")
            return real_send(recipient, kind, payload)

        tasks.send_email = flaky_send
        try:
            broker = InlineBroker(limiter, clock)
            for job in jobs:
                broker.enqueue(job.id)
            broker.run()
        finally:
            tasks.send_email = real_send

        # (1) No job is lost: every job reached a terminal state, all 500 sent.
        statuses = list(EmailJob.objects.values_list("status", flat=True))
        self.assertEqual(len(statuses), 500)
        self.assertTrue(all(s == EmailJob.Status.SENT for s in statuses))
        self.assertEqual(EmailJob.objects.filter(status=EmailJob.Status.SENT).count(), 500)

        # (2) Rate limit never exceeded: no 60s sliding window has > 200 sends.
        times = sorted(broker.send_times)
        for i, start in enumerate(times):
            window_count = sum(1 for t in times[i:] if t < start + window)
            self.assertLessEqual(
                window_count, capacity,
                f"rate exceeded: {window_count} sends within {window}s at t={start}",
            )

        # (3) The intentional failure was retried and eventually succeeded.
        flaky = EmailJob.objects.get(recipient=flaky_recipient)
        self.assertEqual(flaky.status, EmailJob.Status.SENT)
        self.assertGreaterEqual(flaky.attempts, 2)

    def test_permanent_failure_is_dead_lettered(self):
        clock = FakeClock()
        limiter = self._limiter(clock, initial_tokens=200)
        job = EmailJob.objects.create(recipient="", kind=EmailJob.Kind.OTP)
        outcome, _ = tasks.process_email_job(job.id, limiter, retries=0)
        self.assertEqual(outcome, tasks.DEAD)
        job.refresh_from_db()
        self.assertEqual(job.status, EmailJob.Status.DEAD)
        self.assertTrue(DeadLetter.objects.filter(email_job=job).exists())

    def test_transient_failure_exhausts_to_dead_letter(self):
        clock = FakeClock()
        limiter = self._limiter(clock, initial_tokens=200)
        job = EmailJob.objects.create(recipient="x@artikate.test")

        real_send = tasks.send_email
        tasks.send_email = lambda *a, **k: (_ for _ in ()).throw(
            EmailProviderError("always fails")
        )
        try:
            # At the retry ceiling, the job is dead-lettered instead of retried.
            outcome, _ = tasks.process_email_job(
                job.id, limiter, retries=tasks.MAX_RETRIES
            )
        finally:
            tasks.send_email = real_send

        self.assertEqual(outcome, tasks.DEAD)
        job.refresh_from_db()
        self.assertEqual(job.status, EmailJob.Status.DEAD)
