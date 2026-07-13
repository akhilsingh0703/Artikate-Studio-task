class EmailProviderError(Exception):
    """Transient error (5xx, timeout) - safe to retry."""


class PermanentEmailError(Exception):
    """Non-retryable (e.g. bad recipient) - dead-letter straight away."""


def send_email(recipient, kind, payload):
    # Stand-in for the real provider SDK/HTTP call. Kept separate so tests can
    # patch it to force failures.
    if not recipient or "@" not in recipient:
        raise PermanentEmailError(f"invalid recipient: {recipient!r}")
    return {"message_id": f"msg-{recipient}-{kind}"}
