"""
Seed a demo restaurant for quick testing without a full GoPOS backfill.

Usage:
    python -m app.seed              # create/refresh demo tenant
    python -m app.seed --email you@example.com --password secret123

Creates a tenant + owner, products, recipes, ~60 days of orders, work shifts,
overheads and a couple of goals. Onboarding is marked complete so you land
straight in the dashboard.
"""

import argparse
import random
from datetime import datetime, timedelta

from app.core.db import init_db, db_session
from app.core.security import hash_password
from app.core.tenant_context import tenant_scope
from app.models.tenant import Tenant, User, UserRole, OnboardingStep
from app.models import domain as m
from app.repositories.store import TenantRepository
from app.pos.base import POSOrder, POSOrderItem, POSProduct, POSWorkShift

DEMO_EMAIL = "demo@bistro.pl"
DEMO_PASSWORD = "demo12345"

MENU = [
    # name, price, vat, category, recipe(ingredient, qty, unit, cost_per_kg)
    ("Ceviche Cytrusowe", 49.0, 8, "Przystawki", [("Dorada", 140, "g", 62.0), ("Limonka", 1, "szt", 1.5)]),
    ("Tatar Wołowy", 52.0, 8, "Przystawki", [("Polędwica", 120, "g", 95.0)]),
    ("Pasta Vongole", 58.0, 8, "Dania główne", [("Makaron", 120, "g", 8.0), ("Małże", 150, "g", 45.0)]),
    ("Burger Wagyu", 64.0, 8, "Dania główne", [("Wołowina Wagyu", 180, "g", 120.0), ("Bułka", 1, "szt", 2.0)]),
    ("Tiramisu", 28.0, 8, "Desery", [("Mascarpone", 80, "g", 35.0), ("Kawa", 20, "ml", 0.0)]),
    ("Lemoniada", 16.0, 23, "Napoje", [("Cytryna", 2, "szt", 1.2)]),
    ("Wino Czerwone", 95.0, 23, "Napoje", None),  # no recipe — tests coverage gaps
]


def seed(email: str, password: str):
    init_db()
    with db_session() as db:
        existing = db.query(User).filter(User.email == email.lower()).first()
        if existing:
            tenant = db.query(Tenant).filter_by(id=existing.tenant_id).first()
            print(f"Demo tenant already exists: {tenant.name} ({tenant.id}). Wiping data...")
            for model in (m.Order, m.OrderItem, m.DimItem, m.Recipe, m.RecipeIngredient,
                          m.WorkTime, m.Overhead, m.Goal):
                db.query(model).filter_by(tenant_id=tenant.id).delete()
            db.commit()
            tid = tenant.id
        else:
            tenant = Tenant(name="Bistro Demo", city="Warszawa",
                            onboarding_step=OnboardingStep.briefing_active)
            db.add(tenant); db.flush()
            db.add(User(tenant_id=tenant.id, email=email.lower(),
                        password_hash=hash_password(password), role=UserRole.owner))
            db.commit()
            tid = tenant.id
            print(f"Created demo tenant: Bistro Demo ({tid})")

    with tenant_scope(tid), db_session() as db:
        repo = TenantRepository(db, tid)

        # Products
        products = [POSProduct(external_id=f"p{i}", name=n, category_name=c,
                               item_group_name=None, price=pr, tax_id=str(v), tax_rate=float(v),
                               item_type="PRODUCT")
                    for i, (n, pr, v, c, _) in enumerate(MENU)]
        repo.upsert_products(products)

        # Recipes
        for (n, pr, v, c, rec) in MENU:
            if rec:
                repo.save_recipe(n, [{"ingredient": ing, "quantity": q, "unit": u, "cost_per_kg": cpk}
                                     for (ing, q, u, cpk) in rec], selling_price=pr, vat_rate=v)

        # Orders — last 60 days, ~6-14 per open day (Thu-Sun heavier)
        random.seed(42)
        oid = 0
        orders = []
        for d in range(60):
            day = datetime.now() - timedelta(days=d)
            wd = day.weekday()
            base = 12 if wd >= 3 else 6
            n_orders = base + random.randint(-2, 4)
            for _ in range(n_orders):
                hour = random.randint(14, 21)
                ts = day.replace(hour=hour, minute=random.randint(0, 59), second=0, microsecond=0)
                line_items = []
                total = 0.0
                for _ in range(random.randint(1, 4)):
                    n, pr, v, c, _ = random.choice(MENU)
                    qty = random.randint(1, 2)
                    lt = pr * qty
                    total += lt
                    line_items.append(POSOrderItem(
                        external_id=f"oi{oid}_{len(line_items)}", item_external_id=None,
                        name=n, category_name=c, quantity=qty, unit_price=pr, total_price=lt))
                orders.append(POSOrder(
                    external_id=f"o{oid}", closed_at=ts, status="CLOSED", order_type="DINE_IN",
                    total_price=round(total, 2), total_price_net=round(total / 1.08, 2),
                    tip=0.0, items=line_items))
                oid += 1
        repo.bulk_upsert_orders(orders)

        # Work shifts — 2 people per open day
        shifts = []
        sid = 0
        for d in range(60):
            day = datetime.now() - timedelta(days=d)
            if day.weekday() < 3:
                continue
            for name, rate in [("Anna Kowalska", 32.0), ("Piotr Nowak", 38.0)]:
                start = day.replace(hour=13, minute=0, second=0, microsecond=0)
                shifts.append(POSWorkShift(
                    external_id=f"w{sid}", employee_external_id=name[:3], employee_name=name,
                    workplace_name="Sala", start_at=start, end_at=start.replace(hour=22),
                    duration_minutes=540, hourly_rate=rate, to_pay=rate * 9, status="ACCEPTED"))
                sid += 1
        repo.upsert_work_shifts(shifts)

        # Overheads
        repo.set_overhead_cost("Czynsz", 12000)
        repo.set_overhead_cost("Media", 3500)
        repo.set_overhead_cost("Księgowość", 1200)

        # Goals
        repo.create_goal("Marża netto 25%", "net_margin_pct", 25.0, "increase")
        repo.create_goal("Food cost poniżej 32%", "food_cost_pct", 32.0, "decrease")

        print(f"Seeded: {len(orders)} orders, {len([x for x in MENU if x[4]])} recipes, "
              f"{len(shifts)} shifts, 3 overheads, 2 goals")
        print(f"\nLogin:  {email}  /  {password}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--email", default=DEMO_EMAIL)
    ap.add_argument("--password", default=DEMO_PASSWORD)
    args = ap.parse_args()
    seed(args.email, args.password)
