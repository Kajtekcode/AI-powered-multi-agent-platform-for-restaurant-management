#!/usr/bin/env python3
"""
CLI for Restaurant Growth OS.
Preserves the original gopos-agent CLI interface + adds new commands.

Usage:
  python cli.py ping
  python cli.py backfill --days 180
  python cli.py sync --hours 1
  python cli.py ask "Jaki był najlepiej sprzedający się produkt?"
  python cli.py ask --agent financial "Jaki jest realny zysk netto za 30 dni?"
  python cli.py briefing
  python cli.py db-status
"""

import sys
import os

# Allow running from project root
sys.path.insert(0, os.path.dirname(__file__))

import argparse
import json
import logging

logging.basicConfig(
    level=logging.WARNING,  # quiet by default for CLI use
    format="%(asctime)s [%(levelname)s] %(message)s",
)


def _resolve_tenant_id(args=None):
    """Resolve target tenant for CLI ops: --tenant flag, else the first tenant."""
    from app.core.db import db_session
    from app.models.tenant import Tenant
    tid = getattr(args, "tenant", None) if args else None
    if tid:
        return tid
    with db_session() as db:
        t = db.query(Tenant).order_by(Tenant.created_at.asc()).first()
        if not t:
            print("No tenant found. Register one in the app or run: python -m app.seed")
            raise SystemExit(1)
        return t.id


def cmd_ping(args):
    from app.core.gopos_client import get_gopos_client
    client = get_gopos_client()
    ok = client.ping()
    if ok:
        print("✓ GoPOS connection OK")
    else:
        print("✗ GoPOS connection FAILED — check credentials in .env")
        sys.exit(1)


def cmd_backfill(args):
    from app.data.sync import backfill_tenant
    months = getattr(args, "months", 0)
    label = "całą historię" if months == 0 else f"{months} miesięcy wstecz"
    print(f"Starting backfill: {label} (force={args.force})...")
    result = backfill_tenant(_resolve_tenant_id(args), months=months, force=args.force)
    if result["status"] == "success":
        print(f"✓ Backfill complete: {result['orders']} orders, oldest={result.get('oldest')}")
    elif result["status"] == "skipped":
        print(f"⚠ {result['message']}")
    else:
        print(f"✗ {result['message']}")
        sys.exit(1)


def cmd_full_backfill(args):
    from app.data.sync import backfill_tenant
    print("FULL HISTORY backfill — walking backwards in 30-day patches.")
    print("This pulls EVERYTHING since the restaurant opened. May take several minutes...\n")
    import logging as lg
    lg.getLogger("app.data.sync").setLevel(lg.INFO)
    lg.basicConfig(level=lg.INFO, format="%(message)s", force=True)
    result = backfill_tenant(_resolve_tenant_id(args), months=0, force=True)
    if result["status"] == "success":
        print(f"\n✓ COMPLETE: {result['orders']} orders total")
        print(f"  Oldest data found: {result.get('oldest_with_data', '—')}")
        print(f"  Months scanned:    {result.get('months_scanned', 0)}")
    else:
        print(f"✗ {result['message']}")
        sys.exit(1)


def cmd_sync(args):
    from app.data.sync import run_incremental_sync
    print(f"Incremental sync: last {args.hours} hour(s)...")
    result = run_incremental_sync(hours=args.hours)
    print(f"✓ Sync complete: {result.get('orders', 0)} orders updated")


def cmd_ask(args):
    from app.agents.orchestra import (
        run_ceo_agent, run_financial_agent,
        run_menu_agent, run_growth_agent, run_operations_agent
    )
    question = " ".join(args.question)
    agent = args.agent or "ceo"

    print(f"\n[{agent.upper()} AGENT] {question}\n{'─'*60}")

    if agent == "financial":
        response = run_financial_agent(question)
    elif agent == "menu":
        response = run_menu_agent(question)
    elif agent == "growth":
        response = run_growth_agent(question)
    elif agent == "operations":
        response = run_operations_agent(question)
    else:
        response = run_ceo_agent(question, tenant_id=_resolve_tenant_id(args))

    print(response)
    print('─'*60)


def cmd_briefing(args):
    from app.agents.orchestra import generate_daily_briefing
    print("Generating daily briefing (all agents)...\n")
    briefing = generate_daily_briefing()

    print("═"*60)
    print("CEO SUMMARY")
    print("═"*60)
    print(briefing.get("summary", ""))

    for section, title in [
        ("financial", "FINANCIAL ANALYST"),
        ("menu", "MENU & COSTING"),
        ("growth", "GROWTH & MARKETING"),
        ("operations", "OPERATIONS"),
    ]:
        if briefing.get(section):
            print(f"\n{'─'*60}")
            print(title)
            print('─'*60)
            print(briefing[section])


def cmd_db_status(args):
    from app.core.db import db_session
    from app.core.tenant_context import tenant_scope
    from app.repositories.agent_repo import AgentRepo
    tid = _resolve_tenant_id(args)
    with tenant_scope(tid), db_session() as db:
        status = AgentRepo(db, tid).get_data_status()
    print(f"\n{'─'*40}")
    print("DATABASE STATUS")
    print('─'*40)
    for k, v in status.items():
        print(f"  {k:<25} {v}")


def cmd_db_inspect(args):
    """Show daily revenue around a date (tenant-scoped)."""
    from app.core.db import db_session
    from app.core.tenant_context import tenant_scope
    from app.repositories.analytics import AnalyticsRepository
    tid = _resolve_tenant_id(args)
    with tenant_scope(tid), db_session() as db:
        rows = AnalyticsRepository(db, tid).get_sales_by_day(days=400)
    hit = [r for r in rows if r["date"] == args.date]
    print(hit[0] if hit else f"No sales on {args.date}")

def main():
    parser = argparse.ArgumentParser(description="Restaurant Growth OS CLI")
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("ping", help="Test GoPOS connection")

    p_backfill = sub.add_parser("backfill", help="Backfill history (0 = full)")
    p_backfill.add_argument("--months", type=int, default=0, help="0 = cała historia")
    p_backfill.add_argument("--force", action="store_true")

    sub.add_parser("full-backfill", help="Pull ENTIRE history (30-day patches until no more data)")

    p_sync = sub.add_parser("sync", help="Incremental sync")
    p_sync.add_argument("--hours", type=int, default=1)

    p_ask = sub.add_parser("ask", help="Ask the AI agent a question")
    p_ask.add_argument("question", nargs="+")
    p_ask.add_argument("--agent", choices=["ceo", "financial", "menu", "growth", "operations"], default="ceo")

    sub.add_parser("briefing", help="Generate full daily briefing")
    sub.add_parser("db-status", help="Show database status")

    p_inspect = sub.add_parser("db-inspect", help="Inspect orders for a date (YYYY-MM-DD)")
    p_inspect.add_argument("date")

    args = parser.parse_args()

    if args.command == "ping":
        cmd_ping(args)
    elif args.command == "backfill":
        cmd_backfill(args)
    elif args.command == "full-backfill":
        cmd_full_backfill(args)
    elif args.command == "sync":
        cmd_sync(args)
    elif args.command == "ask":
        cmd_ask(args)
    elif args.command == "briefing":
        cmd_briefing(args)
    elif args.command == "db-status":
        cmd_db_status(args)
    elif args.command == "db-inspect":
        cmd_db_inspect(args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
