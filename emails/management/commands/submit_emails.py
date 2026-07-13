"""Submit a burst of email jobs onto the Celery queue (Section 2 demo).

    python manage.py submit_emails --count 500 --fail-rate 0.1

Creates EmailJob rows and enqueues a Celery task per job. Run a worker in
another terminal to process them:

    celery -A config worker -l info
"""

from django.core.management.base import BaseCommand

from emails.models import EmailJob
from emails.tasks import send_email_task


class Command(BaseCommand):
    help = "Enqueue N email jobs to demonstrate the rate-limited queue."

    def add_arguments(self, parser):
        parser.add_argument("--count", type=int, default=500)
        parser.add_argument(
            "--fail-rate",
            type=float,
            default=0.0,
            help="Fraction of jobs given an invalid recipient (dead-lettered).",
        )

    def handle(self, *args, **options):
        count = options["count"]
        fail_rate = options["fail_rate"]
        n_bad = int(count * fail_rate)

        jobs = []
        for i in range(count):
            bad = i < n_bad
            jobs.append(
                EmailJob(
                    recipient="" if bad else f"user{i}@artikate.test",
                    kind=EmailJob.Kind.ORDER_CONFIRMATION,
                    payload={"order_id": i},
                )
            )
        EmailJob.objects.bulk_create(jobs)

        for job in EmailJob.objects.filter(status=EmailJob.Status.QUEUED):
            send_email_task.delay(job.id)

        self.stdout.write(
            self.style.SUCCESS(
                f"Enqueued {count} jobs ({n_bad} intentionally invalid).\n"
                f"Start a worker: celery -A config worker -l info"
            )
        )
