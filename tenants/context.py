"""Per-request tenant context.

Stored in :class:`threading.local` so a queryset built anywhere in the request
lifecycle can discover the active tenant without it being threaded through every
function signature. See ANSWERS.md (Section 3) for why thread-locals are unsafe
under async Django and what ``contextvars`` would change.
"""

import threading
from contextlib import contextmanager

_state = threading.local()


def set_current_tenant(tenant):
    _state.tenant = tenant


def get_current_tenant():
    return getattr(_state, "tenant", None)


def clear_current_tenant():
    if hasattr(_state, "tenant"):
        del _state.tenant


@contextmanager
def tenant_context(tenant):
    """Bind ``tenant`` for the duration of the block, restoring the previous
    value on exit. Handy in tests, management commands and Celery tasks."""
    previous = get_current_tenant()
    set_current_tenant(tenant)
    try:
        yield tenant
    finally:
        if previous is None:
            clear_current_tenant()
        else:
            set_current_tenant(previous)
