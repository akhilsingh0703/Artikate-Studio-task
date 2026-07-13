import heapq
import unittest
from unittest import mock

import redis as redis_lib
from celery.exceptions import Retry
from django.conf import settings
from django.test import TestCase

from . import tasks
from .models import DeadLetter, EmailJob
from .provider import EmailProviderError
from .rate_limiter import RateLimitResult, SlidingWindowRateLimiter


def _redis_available():
    try:
        redis_lib.Redis.from_url(settings.REDIS_URL).ping()
        return True
    except redis_lib.RedisError:
        return False


REDIS_UP = _redis_available()


class FakeClock:
    """Lets us drive the window clock without real sleeping."""

    def __init__(self, start=1_000_000.0):
        self.t = start

    def time(self):
        return self.t

    def advance(self, dt):
        self.t += dt


@unittest.skipUnless(REDIS_UP, "Redis not available")
class SlidingWindowRateLimiterTests(TestCase):
    def _limiter(self, clock, limit=10, window=10):
        lim = SlidingWindowRateLimiter(
            key="test:unit",
            limit=limit,
            window_seconds=window,
            time_func=clock.time,
        )
        lim.reset()
        return lim

    def test_allows_up_to_limit_then_blocks(self):
        clock = FakeClock()
        lim = self._limiter(clock, limit=10, window=10)
        allowed = sum(1 for _ in range(15) if lim.acquire())
        self.assertEqual(allowed, 10)

    def test_frees_up_after_the_window_passes(self):
        clock = FakeClock()
        lim = self._limiter(clock, limit=10, window=10)
        for _ in range(10):
            lim.acquire()
        self.assertFalse(lim.acquire())  # full
        clock.advance(3)
        self.assertFalse(lim.acquire())  # entries still inside the window
        clock.advance(7)  # the first batch is now older than the window
        allowed = sum(1 for _ in range(10) if lim.acquire())
        self.assertEqual(allowed, 10)

    def test_retry_after_points_at_when_a_slot_frees(self):
        clock = FakeClock()
        lim = self._limiter(clock, limit=1, window=10)
        self.assertTrue(lim.acquire())
        result = lim.acquire()
        self.assertFalse(result.allowed)
        self.assertAlmostEqual(result.retry_after, 10, delta=1)

    def test_fails_closed_when_redis_down(self):
        lim = SlidingWindowRateLimiter(
            key="test:down",
            limit=10,
            window_seconds=10,
            client=redis_lib.Redis.from_url("redis://127.0.0.1:6390/0"),
            fail_open=False,
        )
        self.assertFalse(lim.acquire().allowed)


class InlineBroker:
    """Stands in for Celery in tests: pops jobs off a heap, runs the real task
    logic, and re-enqueues retries at clock+countdown (no real sleeping).
    Records the time of each send so we can check the rate limit.
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
                self.clock.t = eta
            before = EmailJob.objects.get(pk=job_id).status
            outcome, info = tasks.process_email_job(job_id, self.limiter, retries)
            if outcome == tasks.SENT and before != EmailJob.Status.SENT:
                self.send_times.append(self.clock.time())
                self.clock.advance(0.001)
            elif outcome == tasks.RETRY_RATE:
                self.enqueue(job_id, retries, eta=self.clock.time() + info)
            elif outcome == tasks.RETRY_ERROR:
                self.enqueue(job_id, retries + 1, eta=self.clock.time() + info)


@unittest.skipUnless(REDIS_UP, "Redis not available")
class QueueBurstTests(TestCase):
    def _limiter(self, clock, limit=200, window=60):
        lim = SlidingWindowRateLimiter(
            key="test:burst",
            limit=limit,
            window_seconds=window,
            time_func=clock.time,
        )
        lim.reset()
        return lim

    def test_500_jobs_no_loss_rate_respected_and_retry(self):
        clock = FakeClock()
        limit, window = 200, 60
        limiter = self._limiter(clock, limit, window)

        flaky_recipient = "user42@artikate.test"  # fails once, then succeeds
        jobs = EmailJob.objects.bulk_create(
            [
                EmailJob(recipient=f"user{i}@artikate.test",
                         kind=EmailJob.Kind.ORDER_CONFIRMATION)
                for i in range(500)
            ]
        )

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

        # 1. nothing lost - every job ended up sent
        statuses = list(EmailJob.objects.values_list("status", flat=True))
        self.assertEqual(len(statuses), 500)
        self.assertTrue(all(s == EmailJob.Status.SENT for s in statuses))

        # 2. no 60s window ever exceeded the cap
        times = sorted(broker.send_times)
        for i, start in enumerate(times):
            window_count = sum(1 for t in times[i:] if t < start + window)
            self.assertLessEqual(window_count, limit)

        # 3. the flaky one retried and eventually went through
        flaky = EmailJob.objects.get(recipient=flaky_recipient)
        self.assertEqual(flaky.status, EmailJob.Status.SENT)
        self.assertGreaterEqual(flaky.attempts, 2)

    def test_permanent_failure_is_dead_lettered(self):
        clock = FakeClock()
        limiter = self._limiter(clock)
        job = EmailJob.objects.create(recipient="", kind=EmailJob.Kind.OTP)
        outcome, _ = tasks.process_email_job(job.id, limiter, retries=0)
        self.assertEqual(outcome, tasks.DEAD)
        job.refresh_from_db()
        self.assertEqual(job.status, EmailJob.Status.DEAD)
        self.assertTrue(DeadLetter.objects.filter(email_job=job).exists())

    def test_transient_failure_exhausts_to_dead_letter(self):
        clock = FakeClock()
        limiter = self._limiter(clock)
        job = EmailJob.objects.create(recipient="x@artikate.test")

        real_send = tasks.send_email
        tasks.send_email = lambda *a, **k: (_ for _ in ()).throw(
            EmailProviderError("always fails")
        )
        try:
            outcome, _ = tasks.process_email_job(
                job.id, limiter, retries=tasks.MAX_RETRIES
            )
        finally:
            tasks.send_email = real_send

        self.assertEqual(outcome, tasks.DEAD)
        job.refresh_from_db()
        self.assertEqual(job.status, EmailJob.Status.DEAD)


class _DenyLimiter:
    """Always rate-limited."""

    def acquire(self):
        return RateLimitResult(False, 0, 0.3)


class RateLimitRetryPolicyTests(TestCase):
    """A rate-limited job must be rescheduled forever, never dropped. The unit
    tests above use an in-process broker that reschedules indefinitely, so they
    can't see a finite retry cap in the real Celery wrapper — this checks it."""

    def test_task_retries_are_unlimited(self):
        # A finite cap silently drops rate-limited jobs once a backlog exceeds
        # it, so the task must allow unlimited retries.
        self.assertIsNone(tasks.send_email_task.max_retries)

    def test_rate_limited_job_reschedules_and_is_not_dropped(self):
        job = EmailJob.objects.create(recipient="x@artikate.test")
        with mock.patch.object(
            tasks.send_email_task, "retry", side_effect=Retry("reschedule")
        ) as retry:
            with self.assertRaises(Retry):
                tasks.send_email_task.run(job.id, limiter=_DenyLimiter())

        self.assertTrue(retry.called)
        job.refresh_from_db()
        self.assertEqual(job.status, EmailJob.Status.QUEUED)
