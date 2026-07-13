from django.db import models


class EmailJob(models.Model):
    """The DB row is the source of truth for a job; Redis/Celery is transport."""

    class Status(models.TextChoices):
        QUEUED = "queued", "Queued"
        SENT = "sent", "Sent"
        FAILED = "failed", "Failed"      # transient, will retry
        DEAD = "dead", "Dead-lettered"   # gave up

    class Kind(models.TextChoices):
        ORDER_CONFIRMATION = "order_confirmation", "Order confirmation"
        OTP = "otp", "OTP"
        ALERT = "alert", "Alert"

    recipient = models.EmailField()
    kind = models.CharField(max_length=32, choices=Kind.choices, default=Kind.ALERT)
    payload = models.JSONField(default=dict, blank=True)

    status = models.CharField(
        max_length=16, choices=Status.choices, default=Status.QUEUED
    )
    attempts = models.PositiveIntegerField(default=0)
    last_error = models.TextField(blank=True, default="")

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [models.Index(fields=["status"])]

    def __str__(self):
        return f"EmailJob #{self.pk} -> {self.recipient} [{self.status}]"


class DeadLetter(models.Model):
    email_job = models.OneToOneField(
        EmailJob, on_delete=models.CASCADE, related_name="dead_letter"
    )
    error = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"DeadLetter for EmailJob #{self.email_job_id}"
