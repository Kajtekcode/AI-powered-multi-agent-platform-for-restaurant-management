"""
Chat API — multi-agent conversation endpoint.

Agent-calling endpoints depend on get_current_tenant, which sets the tenant
context that the orchestra layer reads when building its repositories.
Data endpoints use get_repos directly.
"""

import logging
from typing import List

from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel

from app.core.deps import get_current_tenant
from app.models.tenant import Tenant
from app.api._repos import get_repos, Repos
from app.agents.orchestra import (
    run_ceo_agent, run_financial_agent,
    run_menu_agent, run_growth_agent, run_operations_agent,
    generate_daily_briefing,
)

logger = logging.getLogger(__name__)
router = APIRouter()


class ChatMessage(BaseModel):
    role: str
    content: str


class ChatRequest(BaseModel):
    message: str
    agent: str = "ceo"
    history: List[ChatMessage] = []


class BriefingRequest(BaseModel):
    pass


@router.post("/message")
async def chat_message(req: ChatRequest, tenant: Tenant = Depends(get_current_tenant)):
    """Send a message to the selected agent. get_current_tenant sets the
    tenant context that the agents read."""
    history = [{"role": m.role, "content": m.content} for m in req.history]
    try:
        from app.core.tenant_context import tenant_scope
        agent = req.agent.lower()
        with tenant_scope(tenant.id):
            if agent == "financial":
                response = run_financial_agent(req.message)
            elif agent == "menu":
                response = run_menu_agent(req.message)
            elif agent == "growth":
                response = run_growth_agent(req.message)
            elif agent == "operations":
                response = run_operations_agent(req.message)
            else:
                response = run_ceo_agent(req.message, history if history else None, session="web", tenant_id=tenant.id)
        return {"response": response, "agent": agent}
    except Exception as e:
        logger.error(f"Chat error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/history")
async def get_history(session: str = "web", limit: int = 30, repos: Repos = Depends(get_repos)):
    """Persistent chat history — survives page reloads."""
    return repos.store.get_chat_history(session, limit=limit)


@router.delete("/history")
async def clear_history(session: str = "web", repos: Repos = Depends(get_repos)):
    repos.store.clear_chat_history(session)
    return {"status": "cleared", "session": session}


@router.get("/memories")
async def list_memories(limit: int = 50, repos: Repos = Depends(get_repos)):
    """Long-term memory — facts the CEO agent saved."""
    return repos.store.get_memories(limit=limit)


@router.delete("/memories/{memory_id}")
async def delete_memory(memory_id: str, repos: Repos = Depends(get_repos)):
    repos.store.delete_memory(memory_id)
    return {"status": "deleted", "id": memory_id}


@router.post("/audit/start")
async def audit_start(tenant: Tenant = Depends(get_current_tenant)):
    """Begin or resume the onboarding audit interview."""
    from app.agents.orchestra import start_audit
    return start_audit(tenant_id=tenant.id)


@router.post("/audit/message")
async def audit_message(req: ChatRequest, tenant: Tenant = Depends(get_current_tenant)):
    """Send an answer during the audit interview."""
    from app.agents.orchestra import run_audit_interview
    try:
        return run_audit_interview(req.message, session="web", tenant_id=tenant.id)
    except Exception as e:
        logger.error(f"Audit error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/audit/status")
async def audit_status(repos: Repos = Depends(get_repos)):
    state = repos.store.get_audit_state()
    state["report"] = repos.store.get_setting("audit_report")
    state["report_date"] = repos.store.get_setting("audit_report_date")
    state["profile"] = repos.store.get_profile()
    return state


@router.get("/profile")
async def get_profile(repos: Repos = Depends(get_repos)):
    return repos.store.get_profile()


@router.post("/briefing")
async def generate_briefing(_: BriefingRequest = BriefingRequest(),
                            tenant: Tenant = Depends(get_current_tenant)):
    """Generate a comprehensive daily briefing from all agents."""
    try:
        return generate_daily_briefing()
    except Exception as e:
        logger.error(f"Briefing error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/agents")
async def list_agents():
    return [
        {"id": "ceo", "name": "Doradca CEO", "description": "Strategia i koordynacja wszystkich obszarów", "icon": "🎯"},
        {"id": "financial", "name": "Finanse", "description": "Przychody, marże, rentowność, przepływy", "icon": "💰"},
        {"id": "menu", "name": "Menu", "description": "Food cost, receptury, inżynieria menu", "icon": "🍽️"},
        {"id": "growth", "name": "Wzrost", "description": "Sprzedaż, promocje, ceny, marketing", "icon": "📈"},
        {"id": "operations", "name": "Operacje", "description": "Obsada, wydajność, wąskie gardła", "icon": "⚙️"},
    ]
