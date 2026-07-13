import threading
from contextlib import contextmanager

# Current tenant for the request, kept in thread-local storage so querysets can
# find it without it being passed through every call.
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
    """Bind a tenant for the block, restore the previous one after. Handy in
    tests, management commands and Celery tasks."""
    previous = get_current_tenant()
    set_current_tenant(tenant)
    try:
        yield tenant
    finally:
        if previous is None:
            clear_current_tenant()
        else:
            set_current_tenant(previous)
