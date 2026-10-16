"""Market research endpoints (Round B)."""
import logging

from fastapi import APIRouter, Depends, BackgroundTasks
from sqlalchemy.orm import Session

from app.core.deps import get_current_tenant, get_db
from app.models.tenant import Tenant

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/research", tags=["research"])


@router.post("/run")
def run_research(background_tasks: BackgroundTasks,
                 tenant: Tenant = Depends(get_current_tenant),
                 db: Session = Depends(get_db)):
    """Kick off market research in the background."""
    from app.repositories.store import TenantRepository
    repo = TenantRepository(db, tenant.id)
    # mark running immediately so the UI shows progress without a race
    repo.set_sync_state("research_status", "running")
    repo.set_sync_state("research_label", "uruchamiam…")

    from app.core.research.service import run_research_for_tenant
    background_tasks.add_task(run_research_for_tenant, tenant.id)
    return {"status": "started"}


@router.get("/status")
def research_status(tenant: Tenant = Depends(get_current_tenant),
                    db: Session = Depends(get_db)):
    from app.repositories.store import TenantRepository
    repo = TenantRepository(db, tenant.id)
    import json as _json
    raw_goals = repo.get_setting("research_suggested_goals")
    try:
        suggested = _json.loads(raw_goals) if raw_goals else []
    except Exception:
        suggested = []
    return {
        "status": repo.get_sync_state("research_status") or "idle",
        "label": repo.get_sync_state("research_label") or "",
        "report": repo.get_setting("research_report"),
        "report_date": repo.get_setting("research_report_date"),
        "suggested_goals": suggested,
    }
