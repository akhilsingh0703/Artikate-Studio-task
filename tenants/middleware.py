import jwt
from django.conf import settings

from .context import clear_current_tenant, set_current_tenant
from .models import Tenant


class TenantMiddleware:
    """Resolve the tenant once per request, bind it for the request lifetime,
    and always clear it afterwards so a reused worker thread can't leak a stale
    tenant into the next request.

    Lookup order: JWT `tenant` claim -> X-Tenant header -> subdomain.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        tenant = self._resolve_tenant(request)
        request.tenant = tenant
        set_current_tenant(tenant)
        try:
            return self.get_response(request)
        finally:
            clear_current_tenant()

    def _resolve_tenant(self, request):
        slug = (
            self._slug_from_jwt(request)
            or request.META.get("HTTP_X_TENANT")
            or self._slug_from_subdomain(request)
        )
        if not slug:
            return None
        return Tenant.objects.filter(slug=slug).first()

    def _slug_from_jwt(self, request):
        header = request.META.get("HTTP_AUTHORIZATION", "")
        if not header.startswith("Bearer "):
            return None
        try:
            payload = jwt.decode(
                header.split(" ", 1)[1].strip(),
                settings.TENANT_JWT_SECRET,
                algorithms=["HS256"],
            )
        except jwt.PyJWTError:
            return None
        return payload.get("tenant")

    def _slug_from_subdomain(self, request):
        host = request.get_host().split(":")[0]
        base = settings.TENANT_BASE_DOMAIN
        if host.endswith("." + base):
            return host[: -len("." + base)].split(".")[-1]
        return None
