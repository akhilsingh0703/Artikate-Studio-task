from django.contrib import admin

from .models import Project, Tenant


@admin.register(Tenant)
class TenantAdmin(admin.ModelAdmin):
    list_display = ["id", "name", "slug", "created_at"]
    prepopulated_fields = {"slug": ["name"]}


@admin.register(Project)
class ProjectAdmin(admin.ModelAdmin):
    list_display = ["id", "name", "tenant"]
    # Admin must see every tenant, so use the unscoped manager here.
    def get_queryset(self, request):
        return self.model.all_objects.select_related("tenant")
