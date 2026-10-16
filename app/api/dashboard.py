"""
Dashboard API — all reads/writes go through tenant-scoped repositories
(AnalyticsRepository for reads, TenantRepository for writes), resolved from the
authenticated tenant via get_repos().
"""

import logging

from fastapi import APIRouter, BackgroundTasks, Depends
from pydantic import BaseModel

from app.api._repos import get_repos, Repos

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("/kpis")
async def get_kpis(repos: Repos = Depends(get_repos)):
    """Main KPI cards: revenue, orders, avg ticket, net profit."""
    an = repos.analytics
    summary_30 = an.get_sales_summary(days=30)
    summary_7 = an.get_sales_summary(days=7)

    prev_30 = None
    try:
        prev_30 = an.get_sales_summary_prev(days=30)
    except Exception:
        pass

    net_profit = None
    try:
        net_profit = an.calculate_real_net_profit(repos.store, days=30)
    except Exception:
        pass

    def pct_change(current, previous):
        if not previous or previous == 0:
            return None
        return round((current - previous) / previous * 100, 1)

    prev_rev = prev_30["total_revenue"] if prev_30 and prev_30["order_count"] else None
    prev_orders = prev_30["order_count"] if prev_30 and prev_30["order_count"] else None

    return {
        "revenue_30d": summary_30.get("total_revenue", 0),
        "revenue_7d": summary_7.get("total_revenue", 0),
        "orders_30d": summary_30.get("order_count", 0),
        "avg_ticket_30d": summary_30.get("avg_ticket", 0),
        "revenue_change_pct": pct_change(summary_30.get("total_revenue", 0), prev_rev),
        "orders_change_pct": pct_change(summary_30.get("order_count", 0), prev_orders),
        "net_profit_30d": net_profit["net_profit"] if net_profit else None,
        "net_margin_pct": net_profit["net_margin_pct"] if net_profit else None,
        "food_cost_pct": net_profit["food_cost_pct"] if net_profit else None,
        "net_complete": net_profit["complete"] if net_profit else None,
        "net_basis_note": net_profit["basis_note"] if net_profit else None,
        "labor_has_data": net_profit["labor_has_data"] if net_profit else None,
    }


@router.get("/revenue-chart")
async def get_revenue_chart(days: int = 30, repos: Repos = Depends(get_repos)):
    return repos.analytics.get_sales_by_day(days=days)


@router.get("/hourly-chart")
async def get_hourly_chart(days: int = 14, repos: Repos = Depends(get_repos)):
    return repos.analytics.get_sales_by_hour(days=days)


@router.get("/top-items")
async def get_top_items(metric: str = "revenue", days: int = 30, limit: int = 10,
                        repos: Repos = Depends(get_repos)):
    return repos.analytics.get_top_items(metric=metric, days=days, limit=limit)


@router.get("/channels")
async def get_channels(days: int = 30, repos: Repos = Depends(get_repos)):
    return repos.analytics.get_sales_by_direction(days=days)


@router.get("/employees")
async def get_employee_stats(days: int = 30, repos: Repos = Depends(get_repos)):
    return repos.analytics.get_employee_performance(days=days)


@router.get("/day-of-week")
async def get_dow_stats(days: int = 90, repos: Repos = Depends(get_repos)):
    return repos.analytics.get_day_of_week_performance(days=days)


# ----------------------------- Goals -------------------------------------- #

class GoalBody(BaseModel):
    title: str
    metric: str
    target_value: float | None = None   # null for qualitative ("manual") goals
    direction: str = "increase"
    deadline: str | None = None
    notes: str | None = None


@router.get("/goals")
async def list_goals(status: str = "active", repos: Repos = Depends(get_repos)):
    return repos.analytics.get_goals(repos.store, status=status)


@router.get("/briefing-today")
async def briefing_today(refresh: bool = False, repos: Repos = Depends(get_repos)):
    """Fast advisor briefing for the home page (cached per day)."""
    from app.core.briefing import build_today_briefing
    from app.repositories.agent_repo import AgentRepo
    repo = AgentRepo(repos.store.db, repos.tenant.id)
    return build_today_briefing(repo, restaurant_name=repos.tenant.name or "", force=refresh)


@router.post("/goals")
async def create_goal(body: GoalBody, repos: Repos = Depends(get_repos)):
    gid = repos.analytics.create_goal_tracked(
        repos.store, title=body.title, metric=body.metric, target_value=body.target_value,
        direction=body.direction, deadline=body.deadline, notes=body.notes)
    return {"status": "created", "id": gid}


@router.patch("/goals/{goal_id}")
async def update_goal(goal_id: str, status: str, repos: Repos = Depends(get_repos)):
    repos.store.update_goal_status(goal_id, status)
    return {"status": "updated", "id": goal_id}


@router.delete("/goals/{goal_id}")
async def delete_goal(goal_id: str, repos: Repos = Depends(get_repos)):
    repos.store.delete_goal(goal_id)
    repos.store.set_setting(f"goal_steps:{goal_id}", "")  # clean up steps
    return {"status": "deleted", "id": goal_id}


class StepsBody(BaseModel):
    steps: list  # [{text, done}]


@router.get("/goals/{goal_id}/steps")
async def get_goal_steps(goal_id: str, repos: Repos = Depends(get_repos)):
    import json as _json
    raw = repos.store.get_setting(f"goal_steps:{goal_id}")
    try:
        return {"steps": _json.loads(raw) if raw else []}
    except Exception:
        return {"steps": []}


@router.put("/goals/{goal_id}/steps")
async def set_goal_steps(goal_id: str, body: StepsBody, repos: Repos = Depends(get_repos)):
    import json as _json
    clean = [{"text": str(s.get("text", "")).strip()[:200], "done": bool(s.get("done"))}
             for s in (body.steps or []) if str(s.get("text", "")).strip()]
    repos.store.set_setting(f"goal_steps:{goal_id}", _json.dumps(clean, ensure_ascii=False))
    return {"status": "saved", "steps": clean}


@router.post("/goals/{goal_id}/plan")
async def generate_goal_plan(goal_id: str, repos: Repos = Depends(get_repos)):
    """Advisor generates a concrete step-by-step plan to reach this goal."""
    from app.core.briefing import generate_goal_plan as _plan
    from app.repositories.agent_repo import AgentRepo
    goals = repos.analytics.get_goals(repos.store, status="active")
    goal = next((g for g in goals if g["id"] == goal_id), None)
    if not goal:
        return {"steps": []}
    repo = AgentRepo(repos.store.db, repos.tenant.id)
    steps = _plan(repo, goal)
    # persist as unchecked steps
    import json as _json
    payload = [{"text": s, "done": False} for s in steps]
    repos.store.set_setting(f"goal_steps:{goal_id}", _json.dumps(payload, ensure_ascii=False))
    return {"steps": payload}


# ----------------------------- Today -------------------------------------- #

@router.get("/today")
async def get_today(background_tasks: BackgroundTasks, repos: Repos = Depends(get_repos)):
    """Live TODAY view: revenue, breakeven, projection, pace."""
    return repos.analytics.get_today_live(repos.store)


@router.get("/opening-hours")
async def get_opening_hours(repos: Repos = Depends(get_repos)):
    return repos.store.get_opening_rules()


class OpeningRules(BaseModel):
    rules: list


@router.post("/opening-hours")
async def set_opening_hours(body: OpeningRules, repos: Repos = Depends(get_repos)):
    repos.store.set_opening_rules(body.rules)
    return {"status": "saved", "rules": repos.store.get_opening_rules()}


# ----------------------------- Financials --------------------------------- #

@router.get("/net-profit-range")
async def get_net_profit_range(date_from: str, date_to: str, repos: Repos = Depends(get_repos)):
    return repos.analytics.calculate_real_net_profit_range(repos.store, date_from, date_to)


@router.get("/net-profit")
async def get_net_profit(days: int = 30, repos: Repos = Depends(get_repos)):
    return repos.analytics.calculate_real_net_profit(repos.store, days=days)


@router.get("/food-cost")
async def get_food_cost(days: int = 30, repos: Repos = Depends(get_repos)):
    return repos.analytics.calculate_food_cost_pct(repos.store, days=days)


@router.get("/categories")
async def get_categories(days: int = 30, repos: Repos = Depends(get_repos)):
    return repos.analytics.get_sales_by_category(days=days)


@router.get("/menu-overview")
async def get_menu_overview(days: int = 30, repos: Repos = Depends(get_repos)):
    return repos.analytics.get_menu_overview(repos.store, days=days)


@router.get("/menu-engineering")
async def get_menu_engineering(days: int = 30, repos: Repos = Depends(get_repos)):
    return repos.analytics.get_menu_engineering(repos.store, days=days)


@router.get("/labor")
async def get_labor(days: int = 30, repos: Repos = Depends(get_repos)):
    return repos.analytics.get_labor_cost(days=days)


@router.get("/labor-by-day")
async def get_labor_by_day(days: int = 30, repos: Repos = Depends(get_repos)):
    return repos.analytics.get_labor_by_day(days=days)


@router.get("/monthly")
async def get_monthly(repos: Repos = Depends(get_repos)):
    return repos.analytics.get_sales_by_month()


@router.get("/status")
async def get_status(repos: Repos = Depends(get_repos)):
    """Data status and health check."""
    from app.core.config import settings
    status = repos.analytics.get_data_status(repos.store)
    status["gopos_configured"] = settings.gopos_configured
    status["llm_configured"] = settings.llm_configured
    status["last_sync"] = repos.store.get_sync_state("last_sync")
    return status


class RestaurantProfileBody(BaseModel):
    name: str | None = None
    city: str | None = None


@router.post("/restaurant")
async def update_restaurant(body: RestaurantProfileBody, repos: Repos = Depends(get_repos)):
    """Update the restaurant's display name / city."""
    t = repos.tenant
    if body.name is not None and body.name.strip():
        t.name = body.name.strip()[:120]
    if body.city is not None:
        t.city = body.city.strip()[:80] or None
    repos.store.db.commit()
    return {"status": "saved", "name": t.name, "city": t.city}
