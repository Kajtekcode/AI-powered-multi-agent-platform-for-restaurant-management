"""
Onboarding API — drives the post-registration flow:

  registered → gopos_connected → audit_done → research_done
            → recipes_added → briefing_active

The frontend reads /status to render the step wizard and unlock features.
Steps advance via explicit endpoints (some auto, some user-triggered).
"""

import logging

from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.core.db import get_db
from app.core.deps import get_current_tenant
from app.core.security import encrypt_secret
from app.models.tenant import (Tenant, GoposConnection, OnboardingStep,
                                ConnectionStatus, step_rank, ONBOARDING_ORDER)

logger = logging.getLogger(__name__)
router = APIRouter()


STEP_META = {
    OnboardingStep.registered: {
        "title": "Konto utworzone", "cta": "Połącz GoPOS",
        "desc": "Zacznijmy od połączenia Twojej kasy GoPOS."},
    OnboardingStep.gopos_connected: {
        "title": "GoPOS połączony", "cta": "Rozpocznij audyt",
        "desc": "Pobieramy historię. Teraz krótki wywiad o Twojej restauracji."},
    OnboardingStep.audit_done: {
        "title": "Audyt ukończony", "cta": "Analiza rynku",
        "desc": "Sprawdzamy konkurencję i otoczenie online."},
    OnboardingStep.research_done: {
        "title": "Research gotowy", "cta": "Dodaj przepisy",
        "desc": "Dodaj receptury, by liczyć food cost i marże."},
    OnboardingStep.recipes_added: {
        "title": "Przepisy dodane", "cta": "Włącz briefing",
        "desc": "Wszystko gotowe — włącz poranny briefing."},
    OnboardingStep.briefing_active: {
        "title": "Gotowe!", "cta": "Otwórz dashboard",
        "desc": "Pełen system aktywny. Powodzenia!"},
}


def _advance_to(db: Session, tenant: Tenant, step: OnboardingStep):
    """Move forward only (never regress)."""
    if step_rank(step) > step_rank(tenant.onboarding_step):
        tenant.onboarding_step = step
        db.commit()
        logger.info(f"Tenant {tenant.id} onboarding → {step.value}")


@router.get("/status")
def onboarding_status(tenant: Tenant = Depends(get_current_tenant)):
    cur = tenant.onboarding_step
    completed_rank = step_rank(cur)          # index of the LAST COMPLETED step
    active_rank = completed_rank + 1          # index of the NEXT step to do
    complete = (cur == OnboardingStep.briefing_active)
    steps = []
    for i, s in enumerate(ONBOARDING_ORDER):
        steps.append({
            "key": s.value, "rank": i,
            "title": STEP_META[s]["title"], "desc": STEP_META[s]["desc"],
            "state": "done" if i <= completed_rank else ("current" if i == active_rank else "locked"),
        })
    next_step = (ONBOARDING_ORDER[active_rank]
                 if active_rank < len(ONBOARDING_ORDER) else None)
    return {
        "current_step": cur.value,
        "completed_rank": completed_rank,
        "current_rank": active_rank,          # the step the wizard should highlight
        "complete": complete,
        "next_cta": STEP_META[next_step]["cta"] if next_step else None,
        "steps": steps,
    }


class GoposCredsBody(BaseModel):
    organization_id: str
    client_id: str
    client_secret: str
    history_months: int = 0   # 0 = full available history


@router.post("/gopos/connect")
def connect_gopos(body: GoposCredsBody, background_tasks: BackgroundTasks,
                  tenant: Tenant = Depends(get_current_tenant),
                  db: Session = Depends(get_db)):
    """Verify GoPOS credentials with a live ping, save them encrypted, advance
    the step, and kick off a historical backfill in the background.

    If the credentials are wrong, we DON'T advance — the user gets a clear error.
    """
    # 1. Live ping with the provided credentials BEFORE saving anything as connected.
    from app.pos.gopos import GoPosProvider
    try:
        probe = GoPosProvider(
            client_id=body.client_id.strip(),
            client_secret=body.client_secret.strip(),
            organization_id=body.organization_id.strip(),
        )
        ok = probe.ping()
    except Exception as e:
        logger.warning(f"GoPOS connect probe error for tenant {tenant.id}: {e}")
        ok = False
    if not ok:
        raise HTTPException(
            status_code=400,
            detail="Nie udało się połączyć z GoPOS. Sprawdź Organization ID, Client ID i Client Secret.",
        )

    # 2. Persist encrypted credentials, mark connected.
    conn = db.query(GoposConnection).filter_by(tenant_id=tenant.id).first()
    if conn:
        conn.organization_id = body.organization_id.strip()
        conn.client_id_encrypted = encrypt_secret(body.client_id.strip())
        conn.client_secret_encrypted = encrypt_secret(body.client_secret.strip())
        conn.status = ConnectionStatus.connected
    else:
        conn = GoposConnection(
            tenant_id=tenant.id, organization_id=body.organization_id.strip(),
            client_id_encrypted=encrypt_secret(body.client_id.strip()),
            client_secret_encrypted=encrypt_secret(body.client_secret.strip()),
            status=ConnectionStatus.connected,
        )
        db.add(conn)
    db.commit()
    _advance_to(db, tenant, OnboardingStep.gopos_connected)

    # 3. Kick off historical backfill in the background (user-chosen range).
    from app.data.sync import backfill_tenant
    background_tasks.add_task(backfill_tenant, tenant.id, body.history_months, False)

    return {"status": "connected", "onboarding_step": tenant.onboarding_step.value,
            "backfill": "started"}


@router.get("/gopos/backfill-status")
def gopos_backfill_status(tenant: Tenant = Depends(get_current_tenant),
                          db: Session = Depends(get_db)):
    """Poll the background backfill progress (for the onboarding UI)."""
    from app.repositories.store import TenantRepository
    repo = TenantRepository(db, tenant.id)
    return {
        "status": repo.get_sync_state("backfill_status") or "idle",
        "orders": int(repo.get_sync_state("backfill_orders") or 0),
        "oldest": repo.get_sync_state("backfill_oldest"),
        "current_month": repo.get_sync_state("backfill_label"),
        "order_count": repo.get_order_count(),
    }


class StepBody(BaseModel):
    step: str


@router.post("/advance")
def advance_step(body: StepBody, tenant: Tenant = Depends(get_current_tenant),
                 db: Session = Depends(get_db)):
    """
    Generic advance hook used by audit completion, research, recipes, briefing.
    Validated against the known order; only moves forward.
    """
    try:
        target = OnboardingStep(body.step)
    except ValueError:
        raise HTTPException(status_code=400, detail="Nieznany krok onboardingu")
    _advance_to(db, tenant, target)
    return {"onboarding_step": tenant.onboarding_step.value}
