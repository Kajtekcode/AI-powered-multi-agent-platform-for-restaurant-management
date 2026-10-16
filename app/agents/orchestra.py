"""
Multi-Agent Orchestra
=====================
Five specialized agents that collaborate to provide strategic restaurant insights.

Agent hierarchy:
  CEO / Orchestrator  ← user talks to this one
    ├── Financial Analyst
    ├── Menu & Costing
    ├── Growth & Marketing
    └── Operations

Each agent has:
- A focused system prompt (strategic, not just reporting)
- Access to relevant tools from the local DB
- Ability to call sub-agents and synthesize results
"""

import json
import logging
from datetime import datetime
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Callable, cast

from openai import OpenAI

from app.core.config import settings

logger = logging.getLogger(__name__)


class AgentRole(str, Enum):
    CEO = "ceo"
    FINANCIAL = "financial"
    MENU = "menu"
    GROWTH = "growth"
    OPERATIONS = "operations"


# ---------------------------------------------------------------------------
# System prompts — strategic, opinionated, business-first
# ---------------------------------------------------------------------------

SYSTEM_PROMPTS = {
    AgentRole.CEO: """You are the CEO Advisor and Chief Orchestrator for a restaurant business.
You have a team of specialized AI analysts: Financial, Menu & Costing, Growth & Marketing, and Operations.

Your role:
1. Understand what the owner truly needs (not just what they ask)
2. Call the right specialist agents to gather data
3. Synthesize findings into a cohesive, prioritized action plan
4. Always end with 3 concrete, ranked actions the owner can take TODAY or THIS WEEK
5. Think like a seasoned restaurateur with 20 years of experience

Rules:
- Never hallucinate numbers — use tools to get real data
- FRESH DATA: when the user asks about TODAY, YESTERDAY or "current" numbers, call sync_recent_data FIRST, then query — otherwise local data may be stale.
- INTERNET: you can research the web with web_search + fetch_webpage. Use it for: competitor analysis, reviews (ours and competitors'), local events that may affect traffic, food trends, ingredient price checks. Add the restaurant's city to local queries. Always cite the source URL for web-derived claims, and never present web findings as POS data.
- DISH NAMES: when the user mentions ANY dish, ALWAYS call search_menu first (fuzzy). Menu names rarely match what users type ('Ceviche Cytrusowe' may be 'Ceviche' in the system). Only say a dish doesn't exist after search_menu returns nothing.
- CALENDAR PERIODS: for specific months/years (e.g. 'rok 2025', 'lipiec 2025'), use get_sales_summary_range / get_monthly_revenue for revenue and calculate_real_net_profit_range for profit — never approximate with 'last N days'. Relay the basis_note so the user knows the cost basis. If the result shows order_count=0 or data_complete=false, say clearly that data for that period is missing from the database and suggest running the full-history backfill — do NOT estimate.
- When data is missing, say so explicitly ("No data for X" not "X was zero")
- Be direct, concrete, and business-focused
- Cite the data source for every key number ("based on last 30 days of sales data...")
- If the question isn't clear, make a reasonable assumption and state it
- Polish language is fine when user writes in Polish""",

    AgentRole.FINANCIAL: """You are a Financial Analyst specializing in restaurant P&L.
You have deep expertise in restaurant economics: food cost ratios, labor percentages, contribution margins.

Your role:
1. Analyze revenue trends, margins, and cash flow
2. Calculate real net profit (after food cost AND overhead)
3. Identify financial risks and opportunities
4. Compare periods (WoW, MoM, YoY if data available)
5. Benchmark against industry standards (food cost 28-35%, labor 25-35%, net margin 5-15%)

Be precise with numbers. Always show the calculation, not just the result.
For exact calendar periods (months, quarters, years like 'rok 2025' or 'lipiec 2025') use
get_sales_summary_range, get_monthly_revenue AND calculate_real_net_profit_range — never
approximate with rolling windows. ALWAYS relay the basis_note from net-profit-range results
so the user knows which costs are real vs current-rate estimates. If a period has no data,
state that the database doesn't cover it; never extrapolate.
End with 2-3 financial actions ranked by impact on net profit.
Polish language OK when user writes in Polish.""",

    AgentRole.MENU: """You are a Menu Engineer and Food Cost Specialist.
You understand recipe costing, menu psychology, and product mix optimization.

Your role:
1. Calculate exact food cost per dish (using stored recipes)
2. Identify stars (high sales + high margin), plowhorses (high sales + low margin), puzzles (low sales + high margin), dogs (low everything)
3. Suggest recipe modifications to improve margins
4. Flag dishes missing from recipes (cost unknown = risk)
5. Recommend menu adjustments based on item performance

Menu engineering quadrant:
- Stars → feature prominently, protect margin
- Plowhorses → raise price or reduce cost  
- Puzzles → reposition or promote
- Dogs → consider removing

Be specific: "Ceviche Cytrusowe has 68% food cost — suggest raising price by 8 PLN or reducing portion 15%"

CRITICAL RULES:
- For ANY dish name the user mentions, FIRST call search_menu (fuzzy match) — system names differ from user phrasing.
- Use get_product_catalog to see the full menu with selling prices and VAT rates.
- Margins are VAT-aware: net price = gross price / (1 + VAT/100). Poland: typically 8% food, 23% alcohol/drinks.
- calculate_dish_food_cost returns margin data when a selling price is stored.
Polish language OK when user writes in Polish.""",

    AgentRole.GROWTH: """You are a Growth & Revenue Strategist for the restaurant industry.
You specialize in finding and capturing revenue opportunities.

Your role:
1. Identify underperforming time slots, channels, and items
2. Propose specific promotions, bundles, and upsell strategies
3. Analyze customer patterns and seasonal opportunities
4. Recommend pricing adjustments based on demand patterns
5. Suggest tactical wins for the next 7 days

Be specific: "Tuesday 14:00-17:00 is your weakest 3 hours (avg 340 PLN/hr vs 890 PLN/hr peak) — consider a loyalty hour or delivery push"
Always quantify the opportunity in PLN where possible.
You can research the internet (web_search + fetch_webpage): competitor promos and menus,
local events that drive traffic, seasonal trends. Cite URLs for web findings.
Polish language OK when user writes in Polish.""",

    AgentRole.OPERATIONS: """You are an Operations Manager and efficiency expert for restaurants.
You focus on throughput, staffing, waste reduction, and operational excellence.

Your role:
1. Identify bottlenecks in service (peak hours, slow days)
2. Analyze employee productivity and scheduling gaps  
3. Flag void rates and comp issues
4. Suggest process improvements
5. Optimize staffing to revenue patterns

Be practical: "Your Friday peak (18-21) generates 42% of weekly revenue with the same staff — consider 1 extra person on Fridays"
Polish language OK when user writes in Polish.""",
}


# ---------------------------------------------------------------------------
# Tool definitions (OpenAI function calling format)
# ---------------------------------------------------------------------------

def build_tools() -> List[Dict]:
    """Build all tool schemas available to agents."""
    return [
        {
            "type": "function",
            "function": {
                "name": "save_memory",
                "description": "Save an important fact to LONG-TERM MEMORY (persists forever across all conversations). Use PROACTIVELY whenever the user shares: preferences, business goals, decisions made, plans, constraints, supplier info, or anything worth remembering. Write in the user's language, concise (1-2 sentences).",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "content": {"type": "string", "description": "The fact to remember"},
                        "category": {"type": "string", "enum": ["preference", "goal", "decision", "fact", "general"], "default": "general"},
                    },
                    "required": ["content"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "recall_memories",
                "description": "Search long-term memory for relevant facts. Recent memories are already in your context — use this only to search older ones by keyword.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "Keyword to search"},
                    },
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "save_profile_answer",
                "description": "Save an answer from the onboarding audit interview to the company profile. Call this after the user answers each audit question. The key must match the question's key.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "key": {"type": "string", "description": "Profile key (e.g. 'concept', 'rent', 'competitors')"},
                        "value": {"type": "string", "description": "The user's answer, cleaned up"},
                    },
                    "required": ["key", "value"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "get_audit_progress",
                "description": "Check onboarding audit progress: how many questions answered, what's next. Use to resume the interview.",
                "parameters": {"type": "object", "properties": {}},
            },
        },
        {
            "type": "function",
            "function": {
                "name": "create_goal",
                "description": "Create a business goal with auto-tracked progress. Metrics that auto-update: net_margin_pct, net_profit, revenue, avg_ticket, food_cost_pct, labor_pct. Use 'manual' for goals not tied to POS data. Use after the audit to turn the 90-day plan into trackable goals.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "title": {"type": "string"},
                        "metric": {"type": "string", "enum": ["net_margin_pct", "net_profit", "revenue", "avg_ticket", "food_cost_pct", "labor_pct", "manual"]},
                        "target_value": {"type": "number"},
                        "direction": {"type": "string", "enum": ["increase", "decrease"], "default": "increase"},
                        "deadline": {"type": "string", "description": "YYYY-MM-DD"},
                        "notes": {"type": "string"},
                    },
                    "required": ["title", "metric", "target_value"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "get_goals",
                "description": "List active business goals with current progress (auto-measured from POS data).",
                "parameters": {"type": "object", "properties": {}},
            },
        },
        {
            "type": "function",
            "function": {
                "name": "save_audit_report",
                "description": "Save the final opening audit report + 90-day plan (markdown text) after completing the interview and web research. Shown on the dashboard.",
                "parameters": {
                    "type": "object",
                    "properties": {"report": {"type": "string", "description": "Full report in markdown"}},
                    "required": ["report"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "get_today_status",
                "description": "LIVE status of TODAY: revenue so far, orders, breakeven point (fixed costs + today's staffing, VAT-aware food cost), how far above/below breakeven, end-of-day projection, pace vs same weekday last week, today's top items. Use for 'jak idzie dzisiaj?', 'czy jesteśmy nad progiem?'. Call sync_recent_data first for freshest numbers.",
                "parameters": {"type": "object", "properties": {}},
            },
        },
        {
            "type": "function",
            "function": {
                "name": "web_search",
                "description": "Search the internet (DuckDuckGo). Use for: competitor research, reviews of our restaurant or competitors, local events near the restaurant, food trends, supplier/ingredient prices, anything outside the POS data. Returns titles, URLs and snippets.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "Search query (add city name for local results)"},
                        "max_results": {"type": "integer", "default": 5},
                    },
                    "required": ["query"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "fetch_webpage",
                "description": "Fetch a URL and return its readable text. Use after web_search to read a specific page (competitor menu, review page, event details).",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "url": {"type": "string"}
                    },
                    "required": ["url"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "sync_recent_data",
                "description": "Pull the freshest orders from GoPOS into the local database RIGHT NOW (throttled to max once per 5 min). ALWAYS call this FIRST when the user asks about today, yesterday, or 'current' numbers — otherwise data may be minutes/hours stale.",
                "parameters": {"type": "object", "properties": {}},
            },
        },
        {
            "type": "function",
            "function": {
                "name": "calculate_real_net_profit_range",
                "description": "Net profit for an EXACT calendar period (specific month, quarter, year — e.g. July 2025: date_from='2025-07-01', date_to='2025-07-31'). Returns revenue (real), labor (real shifts from that period), food cost (units from period × current recipes), overhead (current rates prorated) + basis_note explaining the cost basis. ALWAYS relay basis_note to the user. Use THIS (not calculate_real_net_profit) whenever the user names a month/year.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "date_from": {"type": "string", "description": "YYYY-MM-DD"},
                        "date_to": {"type": "string", "description": "YYYY-MM-DD"},
                    },
                    "required": ["date_from", "date_to"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "get_labor_cost",
                "description": "Real labor cost from GoPOS work shifts: total hours, cost (PLN), labor % of revenue, per-employee breakdown. Industry benchmark 25-35%. Default: last N days. For an exact calendar period pass date_from+date_to instead.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "days": {"type": "integer", "default": 30},
                        "date_from": {"type": "string", "description": "YYYY-MM-DD (with date_to → exact period)"},
                        "date_to": {"type": "string", "description": "YYYY-MM-DD"},
                    },
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "search_menu",
                "description": "FUZZY search for a dish/product by name. ALWAYS use this FIRST when the user mentions any dish name — names rarely match exactly (user says 'Ceviche Cytrusowe', system has 'Ceviche'). Returns matching products with menu price, VAT rate, and all-time sales stats.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "Dish name or partial name"},
                        "limit": {"type": "integer", "default": 10},
                    },
                    "required": ["query"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "get_product_catalog",
                "description": "Full menu/product list from GoPOS with selling prices and VAT rates. Use to answer 'what do we sell and for how much'.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "search": {"type": "string", "description": "Optional name filter"},
                        "limit": {"type": "integer", "default": 100},
                    },
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "get_sales_summary_range",
                "description": "Sales summary for an EXACT calendar period (e.g. full year 2025: date_from='2025-01-01', date_to='2025-12-31'). Returns actual data coverage — if data_complete=false or order_count=0, tell the user the data is missing for that period; NEVER estimate.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "date_from": {"type": "string", "description": "YYYY-MM-DD"},
                        "date_to": {"type": "string", "description": "YYYY-MM-DD"},
                    },
                    "required": ["date_from", "date_to"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "get_monthly_revenue",
                "description": "Monthly revenue breakdown over ALL data in the database. Use for yearly summaries, seasonality, and to see exactly which months have data.",
                "parameters": {"type": "object", "properties": {}},
            },
        },
        {
            "type": "function",
            "function": {
                "name": "get_top_items_range",
                "description": "Top-selling items for an exact calendar period (date_from/date_to as YYYY-MM-DD).",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "date_from": {"type": "string"},
                        "date_to": {"type": "string"},
                        "limit": {"type": "integer", "default": 20},
                    },
                    "required": ["date_from", "date_to"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "get_sales_summary",
                "description": "Get revenue, order count, and avg ticket for a time period",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "days": {"type": "integer", "description": "Number of days back from reference now (default 30)", "default": 30}
                    },
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "get_sales_by_day",
                "description": "Get daily revenue breakdown — useful for spotting trends and patterns",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "days": {"type": "integer", "default": 30}
                    },
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "get_sales_by_hour",
                "description": "Get hourly revenue distribution — essential for staffing and scheduling decisions",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "days": {"type": "integer", "default": 14}
                    },
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "get_top_items",
                "description": "Get best-performing menu items by revenue, units, or average price",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "metric": {"type": "string", "enum": ["revenue", "units", "avg_price"], "default": "revenue"},
                        "days": {"type": "integer", "description": "Omit for all-time history"},
                        "limit": {"type": "integer", "default": 15},
                    },
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "get_sales_by_channel",
                "description": "Revenue split by sales channel (dine-in, delivery, takeaway, etc.)",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "days": {"type": "integer", "default": 30}
                    },
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "get_employee_performance",
                "description": "Revenue and order count per employee — for productivity analysis",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "days": {"type": "integer", "default": 30}
                    },
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "get_day_of_week_performance",
                "description": "Revenue by day of week — identify best and worst days",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "days": {"type": "integer", "default": 90}
                    },
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "get_recipe",
                "description": "Get full recipe with ingredients and quantities for a dish",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "dish_name": {"type": "string", "description": "Name of the dish"}
                    },
                    "required": ["dish_name"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "calculate_dish_food_cost",
                "description": "Calculate exact food cost (PLN) for one dish based on its recipe",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "dish_name": {"type": "string"}
                    },
                    "required": ["dish_name"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "calculate_food_cost_pct",
                "description": "Calculate overall food cost % across all dishes with recipes",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "days": {"type": "integer", "default": 30}
                    },
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "get_overhead_costs",
                "description": "Get all fixed overhead costs (rent, energy, payroll, etc.)",
                "parameters": {"type": "object", "properties": {}},
            },
        },
        {
            "type": "function",
            "function": {
                "name": "calculate_real_net_profit",
                "description": "Calculate real net profit: Revenue − Food Cost − Overhead. The true bottom line.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "days": {"type": "integer", "default": 30}
                    },
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "get_data_status",
                "description": "Check data completeness: order count, date range, recipe coverage, last sync",
                "parameters": {"type": "object", "properties": {}},
            },
        },
        {
            "type": "function",
            "function": {
                "name": "get_all_recipes",
                "description": "List all dishes that have recipes loaded in the system",
                "parameters": {"type": "object", "properties": {}},
            },
        },
    ]


# ---------------------------------------------------------------------------
# Tool executor — maps function names → actual DB calls
# ---------------------------------------------------------------------------

def execute_tool(tool_name: str, arguments: Dict[str, Any], store) -> Any:
    try:
        if tool_name == "save_profile_answer":
            store.set_profile_value(arguments["key"], arguments["value"])
            return {"saved": True, "progress": store.get_audit_state()}
        elif tool_name == "get_audit_progress":
            return store.get_audit_state()
        elif tool_name == "create_goal":
            gid = store.create_goal(
                title=arguments["title"], metric=arguments["metric"],
                target_value=arguments["target_value"],
                direction=arguments.get("direction", "increase"),
                deadline=arguments.get("deadline"), notes=arguments.get("notes"))
            return {"created": True, "id": gid}
        elif tool_name == "get_goals":
            return store.get_goals(status="active")
        elif tool_name == "save_audit_report":
            store.set_setting("audit_report", arguments["report"])
            store.set_setting("audit_report_date", datetime.now().isoformat())
            return {"saved": True}
        elif tool_name == "get_today_status":
            return store.get_today_live()
        elif tool_name == "web_search":
            from app.core.web_tools import web_search
            return web_search(arguments["query"], max_results=arguments.get("max_results", 5))
        elif tool_name == "fetch_webpage":
            from app.core.web_tools import fetch_webpage
            return fetch_webpage(arguments["url"])
        elif tool_name == "sync_recent_data":
            from app.data.sync import maybe_sync_tenant
            return maybe_sync_tenant(store.tenant_id, min_interval_minutes=5)
        elif tool_name == "save_memory":
            mem_id = store.save_memory(arguments["content"], arguments.get("category", "general"))
            return {"saved": True, "id": mem_id}
        elif tool_name == "recall_memories":
            return store.get_memories(limit=30, query=arguments.get("query"))
        elif tool_name == "calculate_real_net_profit_range":
            return store.calculate_real_net_profit_range(arguments["date_from"], arguments["date_to"])
        elif tool_name == "get_labor_cost":
            if arguments.get("date_from") and arguments.get("date_to"):
                return store.get_labor_cost_range(arguments["date_from"], arguments["date_to"])
            return store.get_labor_cost(days=arguments.get("days", 30))
        elif tool_name == "search_menu":
            return store.search_menu(arguments["query"], limit=arguments.get("limit", 10))
        elif tool_name == "get_product_catalog":
            return store.get_product_catalog(
                search=arguments.get("search"), limit=arguments.get("limit", 100))
        elif tool_name == "get_sales_summary_range":
            return store.get_sales_summary_range(arguments["date_from"], arguments["date_to"])
        elif tool_name == "get_monthly_revenue":
            return store.get_sales_by_month()
        elif tool_name == "get_top_items_range":
            return store.get_top_items_range(
                arguments["date_from"], arguments["date_to"], limit=arguments.get("limit", 20))
        elif tool_name == "get_sales_summary":
            return store.get_sales_summary(days=arguments.get("days", 30))
        elif tool_name == "get_sales_by_day":
            return store.get_sales_by_day(days=arguments.get("days", 30))
        elif tool_name == "get_sales_by_hour":
            return store.get_sales_by_hour(days=arguments.get("days", 14))
        elif tool_name == "get_top_items":
            return store.get_top_items(
                metric=arguments.get("metric", "revenue"),
                days=arguments.get("days"),
                limit=arguments.get("limit", 15),
            )
        elif tool_name == "get_sales_by_channel":
            return store.get_sales_by_direction(days=arguments.get("days", 30))
        elif tool_name == "get_employee_performance":
            return store.get_employee_performance(days=arguments.get("days", 30))
        elif tool_name == "get_day_of_week_performance":
            return store.get_day_of_week_performance(days=arguments.get("days", 90))
        elif tool_name == "get_recipe":
            return store.get_recipe(arguments["dish_name"])
        elif tool_name == "calculate_dish_food_cost":
            return store.calculate_dish_food_cost(arguments["dish_name"])
        elif tool_name == "calculate_food_cost_pct":
            return store.calculate_food_cost_pct(days=arguments.get("days", 30))
        elif tool_name == "get_overhead_costs":
            return store.get_overhead_costs()
        elif tool_name == "calculate_real_net_profit":
            return store.calculate_real_net_profit(days=arguments.get("days", 30))
        elif tool_name == "get_data_status":
            return store.get_data_status()
        elif tool_name == "get_all_recipes":
            return store.get_all_recipes()
        else:
            return {"error": f"Unknown tool: {tool_name}"}
    except Exception as e:
        logger.error(f"Tool {tool_name} error: {e}")
        return {"error": str(e)}


# ---------------------------------------------------------------------------
# Core agent runner
# ---------------------------------------------------------------------------

def _date_context() -> str:
    """Give the agent today's date + ready-made relative anchors so it can resolve
    'last Sunday', 'yesterday', 'this week' to exact dates for range queries."""
    from datetime import date, timedelta
    today = date.today()
    days_pl = ["poniedziałek", "wtorek", "środa", "czwartek", "piątek", "sobota", "niedziela"]
    # most recent past occurrence of each weekday (0=Mon..6=Sun), strictly before today
    def last(weekday: int) -> date:
        delta = (today.weekday() - weekday) % 7
        delta = delta or 7  # if today is that weekday, take the previous one
        return today - timedelta(days=delta)
    yesterday = today - timedelta(days=1)
    week_mon = today - timedelta(days=today.weekday())
    last_week_mon = week_mon - timedelta(days=7)
    last_week_sun = week_mon - timedelta(days=1)
    lines = [
        "KONTEKST CZASOWY (użyj tych dat do zapytań o okresy — NIE zgaduj dat):",
        f"- Dziś: {today.isoformat()} ({days_pl[today.weekday()]})",
        f"- Wczoraj: {yesterday.isoformat()} ({days_pl[yesterday.weekday()]})",
        f"- Ostatnia niedziela: {last(6).isoformat()}",
        f"- Ostatnia sobota: {last(5).isoformat()}",
        f"- Ostatni piątek: {last(4).isoformat()}",
        f"- Ten tydzień (pon–dziś): {week_mon.isoformat()} … {today.isoformat()}",
        f"- Zeszły tydzień (pon–niedz): {last_week_mon.isoformat()} … {last_week_sun.isoformat()}",
        f"- Bieżący miesiąc: {today.replace(day=1).isoformat()} … {today.isoformat()}",
        "Dla pytań o konkretny dzień/okres użyj get_sales_summary_range z date_from/date_to "
        "(dla jednego dnia date_from = date_to). Jeśli zwróci order_count=0, powiedz wprost, "
        "że dla TEGO dnia brak danych — nie zgaduj.",
    ]
    return "\n".join(lines)


def run_agent(
    role: AgentRole,
    messages: List[Dict],
    extra_system: str = "",
    max_rounds: int = 8,
    repo=None,
) -> str:
    """
    Run one agent to completion using OpenAI tool calling.
    Returns the final text response.
    """
    if not settings.llm_configured:
        return "⚠️ LLM not configured. Set OPENAI_API_KEY in .env"

    client = OpenAI(
        api_key=settings.openai_api_key,
        base_url=settings.openai_base_url,
    )

    system_prompt = SYSTEM_PROMPTS[role]
    system_prompt += "\n\n" + _date_context()
    if extra_system:
        system_prompt += f"\n\n{extra_system}"

    full_messages = [{"role": "system", "content": system_prompt}] + messages
    tools = build_tools()

    for round_num in range(max_rounds):
        # cast(Any, ...): the SDK's typed params (ChatCompletionMessageParam /
        # ChatCompletionToolParam) are TypedDicts — plain dicts are valid at
        # runtime, the cast just tells Pylance we know what we're doing.
        response = client.chat.completions.create(
            model=settings.openai_model,
            messages=cast(Any, full_messages),
            tools=cast(Any, tools),
            tool_choice="auto",
            temperature=0.3,
            max_tokens=2500,
        )

        msg = response.choices[0].message

        # If no tool calls — we have a final answer
        if not msg.tool_calls:
            return msg.content or ""

        # Execute all tool calls
        full_messages.append(msg.model_dump(exclude_unset=True))

        for tc in msg.tool_calls:
            # SDK ≥1.50 splits tool calls into function/custom variants.
            # We only emit function tools — narrow the type explicitly
            # (also silences Pylance reportAttributeAccessIssue).
            fn = getattr(tc, "function", None)
            if fn is None:
                continue

            try:
                arguments = json.loads(fn.arguments)
            except json.JSONDecodeError:
                arguments = {}

            logger.info(f"[{role}] Calling tool: {fn.name}({arguments})")
            result = execute_tool(fn.name, arguments, repo)

            full_messages.append({
                "role": "tool",
                "tool_call_id": tc.id,
                "content": json.dumps(result, ensure_ascii=False, default=str),
            })

    return "⚠️ Max tool call rounds reached. Please rephrase your question."


# ---------------------------------------------------------------------------
# CEO Orchestrator — calls sub-agents and synthesizes
# ---------------------------------------------------------------------------

def run_ceo_agent(
    user_message: str,
    conversation_history: Optional[List[Dict]] = None,
    session: Optional[str] = None,
    tenant_id: Optional[str] = None,
) -> str:
    """
    Main entry point with LONG-TERM MEMORY.
    - Injects saved memories into context (the agent remembers across sessions)
    - When `session` is set ('web'/'telegram'), loads persistent history from DB
      and saves the new exchange — memory never resets.
    """
    from app.core.db import SessionLocal
    from app.repositories.agent_repo import AgentRepo
    _db = SessionLocal()
    store = AgentRepo(_db, tenant_id)
    data_status = store.get_data_status()

    # Restaurant identity for web research context
    from app.core.config import settings as _s
    identity = ""
    if _s.restaurant_name or _s.restaurant_city:
        identity = f"\nRestaurant: {_s.restaurant_name or '(nazwa nieustawiona — zapytaj i zapisz w pamięci)'}"
        if _s.restaurant_city:
            identity += f", city: {_s.restaurant_city}"
        identity += " — use this in web_search queries for local/competitor research."

    # Long-term memory → context
    memories = store.get_memories(limit=30)
    memory_block = ""
    if memories:
        lines = [f"- [{m['category']}] {m['content']}" for m in reversed(memories)]
        memory_block = "\nLONG-TERM MEMORY (facts you saved in past conversations — USE them to personalize advice):\n" + "\n".join(lines)

    context = f"""
Current data context:
- Orders in DB: {data_status.get('order_count', 0):,}
- Data range: {data_status.get('data_from', 'N/A')} → {data_status.get('data_to', 'N/A')}
- Recipes loaded: {data_status.get('recipe_count', 0)}
- Overhead categories: {data_status.get('overhead_count', 0)}
- Reference 'now': {data_status.get('reference_now', 'N/A')}{identity}
{memory_block}

You have access to tools to pull any data you need.
When the question involves financial metrics, ALWAYS call calculate_real_net_profit.
When the question involves menu items, ALWAYS call get_top_items AND calculate_food_cost_pct.
MEMORY: when the user shares a preference, goal, decision, or important business fact —
call save_memory IMMEDIATELY (don't ask permission). This is how you get smarter over time.
"""

    messages: List[Dict] = []
    if session and not conversation_history:
        # Load persistent history (e.g. Telegram, or web after page reload)
        messages.extend(
            {"role": m["role"], "content": m["content"]}
            for m in store.get_chat_history(session, limit=12)
        )
    elif conversation_history:
        messages.extend(conversation_history)
    messages.append({"role": "user", "content": user_message})

    response = run_agent(AgentRole.CEO, messages, extra_system=context, repo=store)

    # Persist the exchange
    if session:
        try:
            store.save_chat_message(session, "user", user_message, "ceo")
            store.save_chat_message(session, "assistant", response, "ceo")
        except Exception as e:
            logger.warning(f"Chat persist failed: {e}")

    _db.close()
    return response


def _audit_ack(user_message: str, question_text: str, repo) -> str:
    """Get a short, warm one-line acknowledgement of the user's answer.
    Falls back to a neutral line if the LLM isn't available — the flow never
    depends on this for correctness."""
    if not settings.llm_configured:
        return "Dzięki, zapisuję."
    try:
        from openai import OpenAI
        client = OpenAI(api_key=settings.openai_api_key, base_url=settings.openai_base_url)
        r = client.chat.completions.create(
            model=settings.openai_model,
            messages=[
                {"role": "system", "content":
                 "Jesteś doradcą restauracji prowadzącym audyt. Użytkownik właśnie odpowiedział "
                 "na pytanie. Odpowiedz JEDNYM krótkim, ciepłym zdaniem potwierdzającym (po polsku). "
                 "Bez zadawania pytań, bez list — tylko potwierdzenie."},
                {"role": "user", "content": f"Pytanie: {question_text}\nOdpowiedź: {user_message}"},
            ],
            temperature=0.5, max_tokens=60,
        )
        return (r.choices[0].message.content or "Dzięki, zapisuję.").strip()
    except Exception as e:
        logger.warning(f"audit ack failed: {e}")
        return "Dzięki, zapisuję."


def run_audit_interview(user_message: str, session: str = "web", tenant_id: Optional[str] = None) -> Dict[str, Any]:
    """Onboarding audit — DETERMINISTIC, code-driven interview.

    The code owns the question order and saves each answer to its key directly
    (no reliance on the LLM to call a tool). The LLM only writes a one-line
    acknowledgement. This prevents repeated questions and wrong progress counts.
    On completion it generates the report + 90-day plan (LLM, with research).
    """
    from app.core.db import db_session
    from app.repositories.agent_repo import AgentRepo
    with db_session() as _db:
        store = AgentRepo(_db, tenant_id)

        # Which question are we answering? The one we last asked (tracked), or
        # the first unanswered as a robust fallback.
        state = store.get_audit_state()
        current_key = store.get_setting("audit_current_key")
        if not current_key or store.get_profile().get(current_key):
            nq = state["next_question"]
            current_key = nq["key"] if nq else None

        # Save the answer to the current question's key (code-driven, reliable).
        question_text = ""
        if current_key:
            q = next((x for x in store.AUDIT_QUESTIONS if x["key"] == current_key), None)
            question_text = q["q"] if q else ""
            cleaned = (user_message or "").strip()
            if cleaned:
                store.set_profile_value(current_key, cleaned)

        # Persist the conversation turn (user side)
        store.save_chat_message(session, "user", user_message, "audit")

        # Recompute after saving; find the next unanswered question
        state = store.get_audit_state()
        next_q = state["next_question"]

        if next_q:
            # Warm ack + next question (question text is fixed, not LLM-generated)
            ack = _audit_ack(user_message, question_text, store)
            store.set_setting("audit_current_key", next_q["key"])
            response = f"{ack}\n\n{next_q['q']}"
            store.save_chat_message(session, "assistant", response, "audit")
            return {"response": response, "progress": state, "complete": False}

        # ── All answered → generate report + research + goals ──
        store.set_setting("audit_current_key", "")
        report_intro = ("To wszystko! Dziękuję — mam pełny obraz. Przygotowuję teraz Twój "
                        "raport otwarcia: diagnozę, plan 90-dniowy i cele. Sprawdzam też "
                        "Twoją konkurencję w internecie. Chwila…")
        # Build the completion prompt for the agent (it has web_search + save_audit_report + create_goal tools)
        profile = store.get_profile()
        profile_block = "\n".join(f"- {k}: {v}" for k, v in profile.items())
        completion_ctx = f"""
The audit interview is COMPLETE. Here is the owner's full profile:
{profile_block}

Now do ALL of the following, in order:
1. Call web_search to research the competitors and city the owner mentioned
   (their reputation, local dining scene). Run 2-4 targeted searches.
2. Call save_audit_report with a full markdown report in POLISH containing:
   (1) Diagnoza — mocne strony i ryzyka (z danych POS + odpowiedzi),
   (2) Analiza rynku i konkurencji (z wyników web_search),
   (3) Plan 90-dniowy — marketing,
   (4) Plan 90-dniowy — finanse i rentowność,
   (5) Rekomendowane cele.
3. Call create_goal for each measurable goal (net_margin_pct, food_cost_pct,
   avg_ticket, revenue, labor_pct) you recommend.
Use their REAL data. Be concrete and specific to this restaurant.
"""
        messages = [{"role": "user", "content": "Audyt zakończony — przygotuj raport, research i cele."}]
        try:
            report_resp = run_agent(AgentRole.CEO, messages, extra_system=completion_ctx, repo=store)
        except Exception as e:
            logger.error(f"audit report generation failed: {e}")
            report_resp = ("Audyt zapisany. Raport przygotuję za chwilę — "
                           "możesz też zapytać mnie o cokolwiek w czacie.")
        final = f"{report_intro}\n\n{report_resp}"
        store.save_chat_message(session, "assistant", final, "audit")

        # Advance onboarding (audit done)
        try:
            from app.models.tenant import OnboardingStep, step_rank, Tenant
            t = store.store.db.query(Tenant).filter_by(id=tenant_id).first()
            if t and step_rank(t.onboarding_step) < step_rank(OnboardingStep.audit_done):
                t.onboarding_step = OnboardingStep.audit_done
                store.store.db.commit()
        except Exception as e:
            logger.warning(f"could not advance onboarding after audit: {e}")

        return {"response": final, "progress": store.get_audit_state(), "complete": True}


def start_audit(tenant_id: Optional[str] = None) -> Dict[str, Any]:
    """Kick off (or resume) the audit — returns the first/next question."""
    from app.core.db import db_session
    from app.repositories.agent_repo import AgentRepo
    with db_session() as _db:
        store = AgentRepo(_db, tenant_id)
        state = store.get_audit_state()
        if state["complete"]:
            return {"response": "Audyt jest już ukończony! Mogę go odświeżyć albo pokazać raport.",
                    "progress": state}
        q = state["next_question"]
        if q:
            store.set_setting("audit_current_key", q["key"])
        intro = ("Świetnie, zróbmy audyt Twojej restauracji — to pomoże mi doradzać Ci dużo "
                 "trafniej. Zadam kilkanaście pytań, możesz przerwać w każdej chwili i wrócić "
                 "później. Zaczynamy:\n\n") if state["answered"] == 0 else "Wracamy do audytu:\n\n"
        return {"response": intro + (q["q"] if q else ""), "progress": state}


def _ctx_repo(tenant_id=None):
    """Build an AgentRepo from the active tenant context (for sub-agent calls)."""
    from app.core.db import SessionLocal
    from app.repositories.agent_repo import AgentRepo
    return AgentRepo(SessionLocal(), tenant_id)


def run_financial_agent(question: str) -> str:
    messages = [{"role": "user", "content": question}]
    return run_agent(AgentRole.FINANCIAL, messages, repo=_ctx_repo())


def run_menu_agent(question: str) -> str:
    messages = [{"role": "user", "content": question}]
    return run_agent(AgentRole.MENU, messages, repo=_ctx_repo())


def run_growth_agent(question: str) -> str:
    messages = [{"role": "user", "content": question}]
    return run_agent(AgentRole.GROWTH, messages, repo=_ctx_repo())


def run_operations_agent(question: str) -> str:
    messages = [{"role": "user", "content": question}]
    return run_agent(AgentRole.OPERATIONS, messages, repo=_ctx_repo())


# ---------------------------------------------------------------------------
# Proactive Daily Briefing — calls multiple agents and synthesizes
# ---------------------------------------------------------------------------

def generate_daily_briefing(tenant_id: Optional[str] = None) -> Dict[str, str]:
    """
    Generate a comprehensive daily briefing by running all agents.
    Returns dict with sections for each area. Tenant-scoped.
    """
    logger.info("Generating daily briefing...")

    from app.core.db import SessionLocal
    from app.repositories.agent_repo import AgentRepo
    store = AgentRepo(SessionLocal(), tenant_id)
    data_status = store.get_data_status()

    if data_status.get("order_count", 0) == 0:
        return {
            "summary": "⚠️ No data available. Run backfill first.",
            "financial": "",
            "menu": "",
            "growth": "",
            "operations": "",
            "actions": "",
        }

    briefing_question = """Generate today's business briefing.
Focus on:
1. Yesterday's performance vs same day last week
2. Key financial metrics (revenue, net profit, food cost %)
3. Top and bottom performing items
4. Specific actions for today and this week
Be concrete with numbers. End with 5 prioritized actions."""

    financial_q = "Analyze last 7 days financial performance. What are the key trends and risks? Focus on net profit margin and food cost %."
    menu_q = "Which menu items are underperforming? Which should be featured? Calculate food cost % and identify the top 3 margin improvement opportunities."
    growth_q = "What are the top 3 revenue opportunities for the next 7 days? Look at channel performance, day-of-week patterns, and pricing opportunities."
    ops_q = "Analyze staffing efficiency and hourly patterns. What operational improvements would have the highest impact this week?"

    sections = {
        "financial": run_financial_agent(financial_q),
        "menu": run_menu_agent(menu_q),
        "growth": run_growth_agent(growth_q),
        "operations": run_operations_agent(ops_q),
    }

    # CEO synthesizes all sections
    synthesis_prompt = f"""Based on these specialist reports, create an executive summary with 5 concrete priority actions:

FINANCIAL ANALYST:
{sections['financial'][:800]}

MENU & COSTING:
{sections['menu'][:800]}

GROWTH & MARKETING:
{sections['growth'][:800]}

OPERATIONS:
{sections['operations'][:800]}

Write a 3-paragraph executive summary + numbered list of 5 priority actions for this week."""

    summary = run_ceo_agent(synthesis_prompt)

    return {
        "summary": summary,
        "financial": sections["financial"],
        "menu": sections["menu"],
        "growth": sections["growth"],
        "operations": sections["operations"],
    }
