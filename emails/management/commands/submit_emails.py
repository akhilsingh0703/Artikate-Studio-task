from django.core.management.base import BaseCommand

from emails.models import EmailJob
from emails.tasks import send_email_task


class Command(BaseCommand):
    help = "Enqueue N email jobs to demo the rate-limited queue."

    def add_arguments(self, parser):
        parser.add_argument("--count", type=int, default=500)
        parser.add_argument(
            "--fail-rate",
            type=float,
            default=0.0,
            help="Fraction of jobs given a bad recipient (they get dead-lettered).",
        )

    def handle(self, *args, **options):
        count = options["count"]
        n_bad = int(count * options["fail_rate"])

        jobs = [
            EmailJob(
                recipient="" if i < n_bad else f"user{i}@artikate.test",
                kind=EmailJob.Kind.ORDER_CONFIRMATION,
                payload={"order_id": i},
            )
            for i in range(count)
        ]
        EmailJob.objects.bulk_create(jobs)

        for job in EmailJob.objects.filter(status=EmailJob.Status.QUEUED):
            send_email_task.delay(job.id)

        self.stdout.write(
            self.style.SUCCESS(
                f"Enqueued {count} jobs ({n_bad} invalid). "
                f"Run a worker: celery -A config worker -l info"
            )
        )
