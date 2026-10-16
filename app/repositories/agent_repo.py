"""
AgentRepo — a thin facade that gives the agent layer ONE object with the same
method names it used to call on the old single store, while routing each call to
the correct tenant-scoped repository (TenantRepository or AnalyticsRepository).

This keeps orchestra.py changes minimal (store.X -> repo.X) and avoids 46
individual reroutes, reducing the risk of mis-wiring during the migration.

Analytics methods that need the store repo (food cost reads recipes) are passed
`self.store` automatically here, so the agent code doesn't worry about it.
"""

from typing import Optional
from sqlalchemy.orm import Session

from app.core.tenant_context import require_tenant
from app.repositories.store import TenantRepository
from app.repositories.analytics import AnalyticsRepository


class AgentRepo:
    def __init__(self, db: Session, tenant_id: Optional[str] = None):
        tid = tenant_id or require_tenant()
        self.store = TenantRepository(db, tid)
        self.analytics = AnalyticsRepository(db, tid)
        self.tenant_id = tid

    # ---- store (writes / CRUD / KV) ------------------------------------- #
    def save_chat_message(self, *a, **k): return self.store.save_chat_message(*a, **k)
    def get_chat_history(self, *a, **k): return self.store.get_chat_history(*a, **k)
    def clear_chat_history(self, *a, **k): return self.store.clear_chat_history(*a, **k)
    def start_new_chat_thread(self, *a, **k): return self.store.start_new_chat_thread(*a, **k)
    def get_chat_threads(self, *a, **k): return self.store.get_chat_threads(*a, **k)
    def get_chat_thread_messages(self, *a, **k): return self.store.get_chat_thread_messages(*a, **k)
    def save_memory(self, *a, **k): return self.store.save_memory(*a, **k)
    def get_memories(self, *a, **k): return self.store.get_memories(*a, **k)
    def delete_memory(self, *a, **k): return self.store.delete_memory(*a, **k)
    def get_profile(self, *a, **k): return self.store.get_profile(*a, **k)
    def set_profile_value(self, *a, **k): return self.store.set_profile_value(*a, **k)
    def ingredient_price_signals(self, *a, **k): return self.store.ingredient_price_signals(*a, **k)
    def get_setting(self, *a, **k): return self.store.get_setting(*a, **k)
    def set_setting(self, *a, **k): return self.store.set_setting(*a, **k)
    def invoice_costs_summary(self, *a, **k): return self.store.invoice_costs_summary(*a, **k)
    def run_query(self, sql: str):
        from app.core.data_query import run_readonly_query
        return run_readonly_query(self.store.db, self.tenant_id, sql)
    def get_sync_state(self, *a, **k): return self.store.get_sync_state(*a, **k)
    def set_sync_state(self, *a, **k): return self.store.set_sync_state(*a, **k)
    def get_recipe(self, *a, **k): return self.store.get_recipe(*a, **k)
    def get_all_recipes(self, *a, **k): return self.store.get_all_recipes(*a, **k)
    def save_recipe(self, *a, **k): return self.store.save_recipe(*a, **k)
    def get_overhead_costs(self, *a, **k): return self.store.get_overhead_costs(*a, **k)
    def get_audit_state(self, *a, **k): return self.store.get_audit_state(*a, **k)
    def update_goal_status(self, *a, **k): return self.store.update_goal_status(*a, **k)
    def delete_goal(self, *a, **k): return self.store.delete_goal(*a, **k)

    @property
    def AUDIT_QUESTIONS(self):
        return self.store.AUDIT_QUESTIONS

    # ---- analytics (reads) — inject store where needed ------------------ #
    def get_sales_summary(self, *a, **k): return self.analytics.get_sales_summary(*a, **k)
    def get_sales_summary_range(self, *a, **k): return self.analytics.get_sales_summary_range(*a, **k)
    def get_sales_by_month(self, *a, **k): return self.analytics.get_sales_by_month(*a, **k)
    def get_sales_by_day(self, *a, **k): return self.analytics.get_sales_by_day(*a, **k)
    def get_sales_by_hour(self, *a, **k): return self.analytics.get_sales_by_hour(*a, **k)
    def get_sales_by_direction(self, *a, **k): return self.analytics.get_sales_by_direction(*a, **k)
    def get_sales_by_category(self, *a, **k): return self.analytics.get_sales_by_category(*a, **k)
    def get_top_items(self, *a, **k): return self.analytics.get_top_items(*a, **k)
    def get_top_items_range(self, *a, **k): return self.analytics.get_top_items_range(*a, **k)
    def get_day_of_week_performance(self, *a, **k): return self.analytics.get_day_of_week_performance(*a, **k)
    def get_employee_performance(self, *a, **k): return self.analytics.get_employee_performance(*a, **k)
    def get_labor_cost(self, *a, **k): return self.analytics.get_labor_cost(*a, **k)
    def get_labor_cost_range(self, *a, **k): return self.analytics.get_labor_cost_range(*a, **k)
    def get_product_catalog(self, *a, **k): return self.analytics.get_product_catalog(*a, **k)
    def search_menu(self, *a, **k): return self.analytics.search_menu(*a, **k)
    def get_today_live(self, *a, **k): return self.analytics.get_today_live(self.store, *a, **k)
    def get_data_status(self, *a, **k): return self.analytics.get_data_status(self.store, *a, **k)
    def calculate_food_cost_pct(self, *a, **k): return self.analytics.calculate_food_cost_pct(self.store, *a, **k)
    def calculate_dish_food_cost(self, *a, **k): return self.analytics.calculate_dish_food_cost(self.store, *a, **k)
    def calculate_real_net_profit(self, *a, **k): return self.analytics.calculate_real_net_profit(self.store, *a, **k)
    def calculate_real_net_profit_range(self, *a, **k): return self.analytics.calculate_real_net_profit_range(self.store, *a, **k)
    def get_goals(self, *a, **k): return self.analytics.get_goals(self.store, *a, **k)
    def create_goal(self, *a, **k): return self.analytics.create_goal_tracked(self.store, *a, **k)
    def measure_spec(self, *a, **k): return self.analytics.measure_spec(self.store, *a, **k)
    def create_spec_goal(self, *a, **k): return self.analytics.create_spec_goal(self.store, *a, **k)
