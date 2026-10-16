"""
Repository dependencies for API handlers.

get_repos() yields a (store, analytics) pair already scoped to the current
tenant (resolved from the JWT by get_current_tenant). Endpoints use this instead
of the old global get_store() singleton.
"""

from dataclasses import dataclass
from fastapi import Depends
from sqlalchemy.orm import Session

from app.core.db import get_db
from app.core.deps import get_current_tenant
from app.models.tenant import Tenant
from app.repositories.store import TenantRepository
from app.repositories.analytics import AnalyticsRepository


@dataclass
class Repos:
    store: TenantRepository
    analytics: AnalyticsRepository
    tenant: Tenant


def get_repos(tenant: Tenant = Depends(get_current_tenant),
              db: Session = Depends(get_db)) -> Repos:
    return Repos(
        store=TenantRepository(db, tenant.id),
        analytics=AnalyticsRepository(db, tenant.id),
        tenant=tenant,
    )
