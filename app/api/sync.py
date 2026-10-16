"""
Sync API — trigger data synchronization from the tenant's POS.
All operations are scoped to the authenticated tenant.
"""
import logging
from fastapi import APIRouter, BackgroundTasks, Depends
from pydantic import BaseModel

from app.core.deps import get_current_tenant
from app.models.tenant import Tenant
from app.api._repos import get_repos, Repos

logger = logging.getLogger(__name__)
router = APIRouter()


class BackfillRequest(BaseModel):
    months: int = 0   # 0 = full history
    force: bool = False


@router.post("/backfill")
async def trigger_backfill(req: BackfillRequest, background_tasks: BackgroundTasks,
                           tenant: Tenant = Depends(get_current_tenant)):
    """Trigger a historical backfill from the tenant's POS."""
    from app.data.sync import backfill_tenant
    background_tasks.add_task(backfill_tenant, tenant.id, req.months, req.force)
    return {"status": "started", "months": req.months, "force": req.force}


@router.post("/incremental")
async def trigger_incremental(background_tasks: BackgroundTasks,
                              tenant: Tenant = Depends(get_current_tenant)):
    """Trigger an incremental sync for this tenant."""
    from app.data.sync import incremental_sync_tenant
    background_tasks.add_task(incremental_sync_tenant, tenant.id, 2)
    return {"status": "started"}


@router.get("/status")
async def sync_status(repos: Repos = Depends(get_repos)):
    """Sync status + data stats for this tenant."""
    status = repos.analytics.get_data_status(repos.store)
    status["last_sync"] = repos.store.get_sync_state("last_sync")
    status["connection_error"] = repos.store.get_sync_state("connection_error") or None
    return status
