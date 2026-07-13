from django.contrib import admin

from .models import DeadLetter, EmailJob


@admin.register(EmailJob)
class EmailJobAdmin(admin.ModelAdmin):
    list_display = ["id", "recipient", "kind", "status", "attempts", "updated_at"]
    list_filter = ["status", "kind"]
    search_fields = ["recipient"]


@admin.register(DeadLetter)
class DeadLetterAdmin(admin.ModelAdmin):
    list_display = ["id", "email_job", "created_at"]
    list_select_related = ["email_job"]
