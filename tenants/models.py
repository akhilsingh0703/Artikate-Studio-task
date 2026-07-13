"""Section 3 — automatic multi-tenant data isolation.

The design goal: a developer who writes ``Order.objects.all()`` and forgets to
add ``.filter(tenant=...)`` must STILL only see the current tenant's rows. We
achieve that by overriding the manager's ``get_queryset`` so the tenant filter
is applied at the source, before any developer-written queryset method runs.
"""

from django.db import models

from .context import get_current_tenant


class Tenant(models.Model):
    name = models.CharField(max_length=200)
    slug = models.SlugField(unique=True)  # used as the subdomain
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.name


class TenantQuerySet(models.QuerySet):
    """QuerySet that can opt out of automatic scoping when explicitly asked."""

    def unscoped(self):
        # Escape hatch for admin/superuser/cross-tenant jobs. Naming it
        # explicitly makes any bypass grep-able in code review.
        return self.model._base_manager.get_queryset()


class TenantManager(models.Manager):
    """Manager that injects ``.filter(tenant=current_tenant)`` automatically.

    ``get_queryset`` is the single chokepoint every ORM call flows through
    (``.all()``, ``.filter()``, ``.get()``, related lookups, ...), so scoping
    here cannot be bypassed by forgetting a filter at the call site.
    """

    def get_queryset(self):
        qs = TenantQuerySet(self.model, using=self._db)
        tenant = get_current_tenant()
        if tenant is None:
            # Fail CLOSED: with no tenant bound, expose nothing rather than
            # risk leaking another tenant's data. See ANSWERS.md.
            return qs.none()
        return qs.filter(tenant=tenant)


class TenantScopedModel(models.Model):
    """Abstract base for every tenant-owned model.

    ``objects`` is tenant-scoped and used by application code. ``all_objects``
    is the unscoped default manager, used only by migrations, the admin and
    deliberate cross-tenant tooling.
    """

    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE)

    objects = TenantManager()
    all_objects = models.Manager()

    class Meta:
        abstract = True
        base_manager_name = "all_objects"


class Project(TenantScopedModel):
    """A representative tenant-owned resource used by the isolation tests."""

    name = models.CharField(max_length=200)

    def __str__(self):
        return self.name
