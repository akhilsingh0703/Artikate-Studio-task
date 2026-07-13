"""A stand-in for the third-party email provider.

Kept separate so tests can monkeypatch ``send_email`` to force deterministic
failures without touching the Celery task logic.
"""


class EmailProviderError(Exception):
    """Transient provider error (5xx, timeout) — safe to retry."""


class PermanentEmailError(Exception):
    """Non-retryable error (e.g. malformed recipient) — dead-letter directly."""


def send_email(recipient, kind, payload):
    """Pretend to hand the message to the provider.

    In a real system this calls the provider SDK/HTTP API. Here it just
    validates and returns a fake message id.
    """
    if not recipient or "@" not in recipient:
        raise PermanentEmailError(f"invalid recipient: {recipient!r}")
    return {"message_id": f"msg-{recipient}-{kind}"}
