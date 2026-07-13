from rest_framework.decorators import api_view
from rest_framework.response import Response

from .models import Project


@api_view(["GET"])
def project_list(request):
    # Project.objects is tenant-scoped, so this returns only the current
    # tenant's rows (or nothing when no tenant is resolved). The tenant comes
    # from the JWT / X-Tenant header / subdomain via TenantMiddleware.
    tenant = getattr(request, "tenant", None)
    projects = list(Project.objects.values("id", "name"))
    return Response(
        {
            "tenant": tenant.slug if tenant else None,
            "count": len(projects),
            "projects": projects,
        }
    )
