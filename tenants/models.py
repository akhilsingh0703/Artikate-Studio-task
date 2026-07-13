from django.db import models

from .context import get_current_tenant


class Tenant(models.Model):
    name = models.CharField(max_length=200)
    slug = models.SlugField(unique=True)  # used as the subdomain
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.name


class TenantQuerySet(models.QuerySet):
    def unscoped(self):
        # escape hatch for admin / cross-tenant jobs; grep-able on purpose
        return self.model._base_manager.get_queryset()


class TenantManager(models.Manager):
    def get_queryset(self):
        # get_queryset is the one place every ORM call goes through (.all,
        # .filter, .get, related lookups), so filtering here can't be bypassed
        # by forgetting a filter at the call site.
        qs = TenantQuerySet(self.model, using=self._db)
        tenant = get_current_tenant()
        if tenant is None:
            # no tenant bound -> show nothing rather than risk a leak
            return qs.none()
        return qs.filter(tenant=tenant)


class TenantScopedModel(models.Model):
    """Base for tenant-owned models. `objects` is scoped; `all_objects` isn't
    (migrations, admin, deliberate cross-tenant tooling)."""

    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE)

    objects = TenantManager()
    all_objects = models.Manager()

    class Meta:
        abstract = True
        base_manager_name = "all_objects"


class Project(TenantScopedModel):
    name = models.CharField(max_length=200)

    def __str__(self):
        return self.name
