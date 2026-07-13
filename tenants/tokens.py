"""Helper to mint tenant-scoped JWTs for local testing / demos."""

import jwt
from django.conf import settings


def make_tenant_token(slug, **extra_claims):
    payload = {"tenant": slug, **extra_claims}
    return jwt.encode(payload, settings.TENANT_JWT_SECRET, algorithm="HS256")
