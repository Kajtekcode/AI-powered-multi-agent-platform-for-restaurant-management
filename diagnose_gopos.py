#!/usr/bin/env python3
"""
Run this on your machine to verify GoPOS API works correctly.
  python diagnose_gopos.py

Prints raw API responses so you can confirm data is flowing.
"""
import sys, os, json, datetime
sys.path.insert(0, os.path.dirname(__file__))
from dotenv import load_dotenv
load_dotenv()

import httpx
from app.core.config import settings

BASE = settings.gopos_base_url
ORG  = settings.gopos_organization_id

print("=" * 60)
print("1 — AUTH")
r = httpx.post(f"{BASE}/oauth/token", data={
    "grant_type":      "organization",
    "client_id":       settings.gopos_client_id,
    "client_secret":   settings.gopos_client_secret,
    "organization_id": ORG,
}, timeout=15)
print(f"  status: {r.status_code}")
if r.status_code != 200:
    print(f"  FAILED: {r.text[:400]}")
    sys.exit(1)
TOKEN = r.json()["access_token"]
HDR   = {"Authorization": f"Bearer {TOKEN}"}
print("  OK ✓")

def get(label, path, **params):
    url = f"{BASE}{path}"
    print(f"\n{'='*60}")
    print(f"{label}")
    print(f"  GET {url}")
    print(f"  params: {params}")
    r = httpx.get(url, headers=HDR, params=params, timeout=20)
    print(f"  status: {r.status_code}")
    if r.status_code != 200:
        print(f"  ERROR: {r.text[:300]}")
        return None
    data = r.json()
    items = data.get("data", []) if isinstance(data, dict) else data
    print(f"  items in 'data': {len(items)}")
    if items:
        print(f"  first item keys: {list(items[0].keys())[:12]}")
        print(f"  first item sample:")
        print("  " + json.dumps(items[0], indent=4, default=str, ensure_ascii=False)[:600])
    else:
        print(f"  full response: {json.dumps(data, default=str)[:400]}")
    return data

get("2 — ITEMS (first 2)",   f"/api/v3/{ORG}/items",     size=2, include="category")
get("3 — EMPLOYEES (first 2)",f"/api/v3/{ORG}/employees", size=2)

from_7d = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=7)).isoformat()
get("4 — ORDERS last 7 days (first 2, status=CLOSED)",
    f"/api/v3/{ORG}/orders", size=2, status="CLOSED",
    closed_at_from=from_7d, include="items,items.product,employee")

get("5 — ORDERS no status filter (first 2)",
    f"/api/v3/{ORG}/orders", size=2)

# ── 6. FULL order with items[] expanded (untruncated) ──────────────────────
print(f"\n{'='*60}")
print("6 — ORDER ITEMS structure (full dump of 1 order's items)")
r = httpx.get(f"{BASE}/api/v3/{ORG}/orders", headers=HDR,
              params={"size": 5, "status": "CLOSED",
                      "include": "items,items.product,items.product.category,employee"},
              timeout=20)
if r.status_code == 200:
    orders = r.json().get("data", [])
    sample = next((o for o in orders if o.get("items")), orders[0] if orders else None)
    if sample:
        print(f"  order id: {sample.get('id')}  employee field: {json.dumps(sample.get('employee'), ensure_ascii=False)}")
        items = sample.get("items") or []
        print(f"  items count: {len(items)}")
        if items:
            print("  FIRST ITEM (full):")
            print("  " + json.dumps(items[0], indent=4, default=str, ensure_ascii=False))
        else:
            print("  ⚠ to zamówienie nie ma pozycji w 'items' — sprawdź nazwę include")
else:
    print(f"  ERROR {r.status_code}: {r.text[:200]}")

# ── 7. TAXES ───────────────────────────────────────────────────────────────
get("7 — TAXES (stawki VAT)", f"/api/v3/{ORG}/taxes", size=10)

# ── 8. WORK TIMES (grafiki/koszty pracy) — testujemy nazwy parametrów ──────
wt_from = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=30)).date().isoformat()
wt_to   = datetime.datetime.now(datetime.timezone.utc).date().isoformat()
print(f"\n{'='*60}")
print("8 — WORK_TIMES (grafiki) — próba z date_from/date_to")
r = httpx.get(f"{BASE}/api/v3/{ORG}/work_times", headers=HDR,
              params={"size": 2, "date_from": wt_from, "date_to": wt_to,
                      "include": "employee,workplace"}, timeout=20)
print(f"  GET /api/v3/{ORG}/work_times?date_from={wt_from}&date_to={wt_to}")
print(f"  status: {r.status_code}")
if r.status_code == 200:
    items = r.json().get("data", [])
    print(f"  items in 'data': {len(items)}")
    if items:
        print("  FIRST WORK_TIME (full):")
        print("  " + json.dumps(items[0], indent=4, default=str, ensure_ascii=False))
    else:
        print("  ⚠ pusto — może inne nazwy parametrów dat? (spróbuj bez filtra)")
        r2 = httpx.get(f"{BASE}/api/v3/{ORG}/work_times", headers=HDR, params={"size": 2}, timeout=20)
        print(f"  bez filtra → status {r2.status_code}, items: {len(r2.json().get('data', [])) if r2.status_code==200 else 'ERR'}")
        if r2.status_code == 200 and r2.json().get("data"):
            print("  " + json.dumps(r2.json()["data"][0], indent=4, default=str, ensure_ascii=False)[:600])
else:
    print(f"  ERROR: {r.text[:300]}")
    print("  → endpoint /work_times może nazywać się inaczej. Sprawdź w Swaggerze sekcję 'work'.")

# ── 9. SZUKANIE endpointu RCP / czasu pracy (próba kilku nazw) ─────────────
print(f"\n{'='*60}")
print("9 — SZUKANIE endpointu czasu pracy (RCP) — próbujemy kilku nazw")
candidates = [
    "work_times", "worktimes", "work_time", "employee_work_times",
    "employee_activities", "employee_activity", "attendances", "attendance",
    "shifts", "work_shifts", "clock_entries", "time_entries", "workdays",
    "employee_shifts", "rcp", "presence", "presences",
]
found = []
for ep in candidates:
    try:
        rr = httpx.get(f"{BASE}/api/v3/{ORG}/{ep}", headers=HDR, params={"size": 1}, timeout=12)
        n = len(rr.json().get("data", [])) if rr.status_code == 200 else None
        flag = "✓ ISTNIEJE" if rr.status_code == 200 else ("404" if rr.status_code == 404 else f"status {rr.status_code}")
        print(f"  /{ep:<22} → {flag}" + (f"  (data: {n})" if n is not None else ""))
        if rr.status_code == 200:
            found.append((ep, n, rr))
    except Exception as e:
        print(f"  /{ep:<22} → błąd {str(e)[:40]}")

# For any endpoint that exists AND returns data, dump a sample
for ep, n, rr in found:
    if n and n > 0:
        print(f"\n  PRÓBKA z /{ep}:")
        print("  " + json.dumps(rr.json()["data"][0], indent=4, default=str, ensure_ascii=False)[:700])

print("\n" + "="*60)
print("DONE — wklej cały wynik. Punkty 6/7/8/9 są kluczowe do dostrojenia mapperów.")

# ── 10. WORK_INFO per employee — prawdziwe źródło RCP (check_in/out) ────────
print(f"\n{'='*60}")
print("10 — EMPLOYEE work_info (RCP: check_in/check_out → przepracowany czas)")
r = httpx.get(f"{BASE}/api/v3/{ORG}/employees", headers=HDR, params={"size": 20}, timeout=20)
emps = r.json().get("data", []) if r.status_code == 200 else []
print(f"  pracownicy: {len(emps)}")
shown = 0
for e in emps:
    eid = e.get("id")
    try:
        wr = httpx.get(f"{BASE}/api/v3/{ORG}/employees/{eid}/work_info", headers=HDR, timeout=15)
        if wr.status_code == 200:
            data = wr.json()
            body = data.get("data", data)
            # only print employees that actually have work data
            txt = json.dumps(body, default=str, ensure_ascii=False)
            if txt not in ("null", "{}", "[]") and shown < 3:
                print(f"\n  EMPLOYEE id={eid} ({e.get('name')}) → work_info:")
                print("  " + json.dumps(body, indent=4, default=str, ensure_ascii=False)[:800])
                shown += 1
        else:
            print(f"  id={eid} work_info → status {wr.status_code}")
    except Exception as ex:
        print(f"  id={eid} work_info → błąd {str(ex)[:40]}")
if shown == 0:
    print("  ⚠ Żaden pracownik nie ma danych work_info — RCP może nie być wypełnione,")
    print("    albo czas pracy trafia gdzie indziej. Wklej też próbkę z punktu 9 jeśli coś istniało.")

# ── 11. ORDERS report (alternatywne źródło, paginacja po dacie) ────────────
print(f"\n{'='*60}")
print("11 — sprawdzenie zakresu historii (najstarsze zamówienie)")
r = httpx.get(f"{BASE}/api/v3/{ORG}/orders", headers=HDR,
              params={"size": 1, "status": "CLOSED", "sort": "closed_at,asc"}, timeout=20)
if r.status_code == 200:
    d = r.json().get("data", [])
    if d:
        print(f"  najstarsze CLOSED zamówienie: closed_at={d[0].get('closed_at')} (id={d[0].get('id')})")
        print("  → tyle historii sięga GoPOS dla tej organizacji")
else:
    print(f"  status {r.status_code}")

print("\n" + "="*60)
print("KONIEC — wklej punkty 10 i 11.")
