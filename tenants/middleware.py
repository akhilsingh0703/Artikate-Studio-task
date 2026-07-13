"""Tenant resolution middleware (Section 3).

Resolves the tenant once per request and binds it to the thread-local context
for the full request lifecycle, then *always* clears it in a ``finally`` block
so a pooled/reused worker thread can never leak a stale tenant into the next
request.

Resolution order:
1. ``Authorization: Bearer <jwt>`` header with a ``tenant`` claim.
2. ``X-Tenant`` header (convenience for local testing).
3. Subdomain of the Host header (``acme.artikate.test`` -> slug ``acme``).
"""

import jwt
from django.conf import settings

from .context import clear_current_tenant, set_current_tenant
from .models import Tenant


class TenantMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        tenant = self._resolve_tenant(request)
        request.tenant = tenant
        set_current_tenant(tenant)
        try:
            return self.get_response(request)
        finally:
            # Critical: unbind even on exceptions so the next request served by
            # this thread starts with a clean context.
            clear_current_tenant()

    def _resolve_tenant(self, request):
        slug = self._slug_from_jwt(request) or self._slug_from_header(request) \
            or self._slug_from_subdomain(request)
        if not slug:
            return None
        return Tenant.objects.filter(slug=slug).first()

    def _slug_from_jwt(self, request):
        header = request.META.get("HTTP_AUTHORIZATION", "")
        if not header.startswith("Bearer "):
            return None
        token = header.split(" ", 1)[1].strip()
        try:
            payload = jwt.decode(
                token, settings.TENANT_JWT_SECRET, algorithms=["HS256"]
            )
        except jwt.PyJWTError:
            return None
        return payload.get("tenant")

    def _slug_from_header(self, request):
        return request.META.get("HTTP_X_TENANT") or None

    def _slug_from_subdomain(self, request):
        host = request.get_host().split(":")[0]
        base = settings.TENANT_BASE_DOMAIN
        if host.endswith("." + base):
            return host[: -len("." + base)].split(".")[-1]
        return None
