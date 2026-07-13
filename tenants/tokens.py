import jwt
from django.conf import settings


def make_tenant_token(slug, **extra_claims):
    """Mint a tenant JWT for local testing / demos."""
    return jwt.encode(
        {"tenant": slug, **extra_claims},
        settings.TENANT_JWT_SECRET,
        algorithm="HS256",
    )
