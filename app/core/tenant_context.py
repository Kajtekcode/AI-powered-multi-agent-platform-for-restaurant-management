"""
Tenant context — the active tenant_id for the current request or background task.

Set by:
  - auth middleware (from the verified JWT) on each HTTP request
  - scheduler / telegram bot (explicitly per tenant) for background work

Read by the repository layer to scope every query. NEVER set from user-supplied
request body — only from a verified token or trusted server loop.
"""

from contextlib import contextmanager
from contextvars import ContextVar
from typing import Optional

_current_tenant_id: ContextVar[Optional[str]] = ContextVar("current_tenant_id", default=None)


def set_tenant(tenant_id: Optional[str]):
    return _current_tenant_id.set(tenant_id)


def get_tenant() -> Optional[str]:
    return _current_tenant_id.get()


def require_tenant() -> str:
    tid = _current_tenant_id.get()
    if not tid:
        raise RuntimeError("No active tenant in context — auth/middleware misconfigured.")
    return tid


@contextmanager
def tenant_scope(tenant_id: str):
    """Run a block in the context of a specific tenant (schedulers, bots, scripts)."""
    token = _current_tenant_id.set(tenant_id)
    try:
        yield
    finally:
        _current_tenant_id.reset(token)
