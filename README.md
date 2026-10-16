# 🍽️ Restaurant Growth OS

**A SaaS platform for restaurants — your team of AI managers.** Five specialized
agents analyze data from the GoPOS point-of-sale system, audit your business, track
your break-even point, calculate your actual net profit, and prepare a practical
action plan every morning.

Stack: **FastAPI · SQLAlchemy · PostgreSQL (Neon) · Alpine.js + Tailwind · OpenAI/Grok**.
The **multi-tenant** architecture supports multiple restaurants with full data
isolation, and the app can be installed as a **PWA**.

---

## Table of contents
1. [Requirements](#requirements)
2. [Quick start locally (SQLite)](#quick-start-locally-sqlite)
3. [Run with Neon (Postgres)](#run-with-neon-postgres)
4. [Step-by-step test](#step-by-step-test)
5. [Architecture](#architecture)
6. [Key features](#key-features)
7. [Security and isolation](#security-and-isolation)
8. [CLI commands](#cli-commands)
9. [Troubleshooting](#troubleshooting)

---

## Requirements
- **Python 3.11+** (tested on 3.11–3.14)
- A **Neon** account (free serverless Postgres) — or any Postgres instance
- An **OpenAI** or **xAI/Grok** API key (for AI agents)
- **GoPOS API** credentials for your restaurant (Organization ID, Client ID, Client Secret)

---

## Quick start locally (SQLite)

The quickest way to try the app — no Postgres required; it uses built-in SQLite.

```bash
# 1. Environment
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# 2. Configuration
cp .env.example .env
#    Set at least these values in .env:
#      JWT_SECRET=<openssl rand -hex 32>
#      FERNET_KEY=<python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())">
#      OPENAI_API_KEY=... (+ OPENAI_BASE_URL/OPENAI_MODEL if using Grok)
#    DATABASE_URL is optional — the app will use sqlite:///data/dev.db

# 3. Database schema
alembic upgrade head

# 4. (Optional) demo data — "Bistro Demo" restaurant with 659 orders
python -m app.seed
#    Login: demo@bistro.pl / demo12345

# 5. Start
python run.py
#    → http://localhost:8000
```

Without demo data, you’ll see the registration screen and can complete the full onboarding process.

---

## Run with Neon (Postgres)

This is the intended production setup, with full multi-tenant isolation and RLS.

### 1. Create a Neon database
Create a project at [neon.tech](https://neon.tech) and copy the **connection string**
(including `?sslmode=require`).

### 2. Configure `.env`
```bash
cp .env.example .env
```
Set these values in `.env`:
```ini
DATABASE_URL=******YOUR_NEON_HOST/DBNAME?sslmode=require
JWT_SECRET=<openssl rand -hex 32>
FERNET_KEY=<python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())">
COOKIE_SECURE=true          # when using HTTPS
OPENAI_API_KEY=...           # or a Grok key + OPENAI_BASE_URL + OPENAI_MODEL
```

> ⚠️ **FERNET_KEY** should be generated **once** and must not be changed after saving GoPOS credentials; otherwise, the stored secrets cannot be decrypted.
>
> ⚠️ If you used a temporary `DATABASE_URL` with a plaintext password, **rotate the password** in the Neon dashboard before going live.

### 3. Run migrations (create the schema and enable RLS)
```bash
pip install -r requirements.txt
alembic upgrade head
```
On Postgres, the final migration (`c1f5a7e2b9d0`) **actually enables Row-Level Security**
on all data tables. On SQLite, this migration is skipped (no-op).

### 4. Start
```bash
python run.py --prod
```

---

## Step-by-step test

After deployment, it’s worth walking through the complete user journey:

1. **Register** — go to `/`. Without a session, you’ll be redirected to `/login`. Click
   “Create an account” and enter the restaurant name, city, email, and password (at least
   8 characters).
2. **Onboarding — step 2: GoPOS** — the setup wizard will take you to the connection
   screen. Enter the Organization ID, Client ID, and Client Secret from your GoPOS
   dashboard. The credentials will be **encrypted** and saved to the database; a
   background sync of your sales history will begin.
3. **Onboarding — step 3: Audit** — the CEO Agent will ask several questions about your
   business (requires an LLM to be configured). You can pause and return later; your
   progress is saved.
4. **Remaining steps** — market analysis, recipes (you can add them from a menu photo),
   and enabling the morning briefing. After the final step, you’ll arrive at the dashboard.
5. **Dashboard** — check the tabs: “Today” (live break-even point), “Overview” (KPIs and
   charts), “Menu” (food cost and menu engineering), and “Chat” (talk to the agents).
   The sidebar shows the restaurant name and email, **Log out**, and the light/dark mode
   toggle.
6. **Log out / log in** — verify that the session works and that `/` requires you to log in
   again after logging out.
7. **PWA** — on your phone, open the app in a browser and select “Add to Home Screen.”
   It will launch in full-screen mode with its own icon.
8. **Isolation (optional)** — create a second account for a different restaurant. Verify
   that it cannot access the first restaurant’s data — either through the app or (on
   Postgres) at the database level.

---

## Architecture

```
app/
├── main.py              # FastAPI: page routing, onboarding gating, PWA, lifespan
├── core/
│   ├── config.py        # settings (env)
│   ├── db.py            # engine + sessions (Neon/SQLite), init_db
│   ├── security.py      # bcrypt, JWT, Fernet (POS credential encryption)
│   ├── deps.py          # FastAPI dependencies: current user/tenant, gating
│   ├── tenant_context.py# ContextVar for the current tenant
│   ├── scheduler.py     # auto-sync + morning briefing (across all tenants)
│   ├── notifications.py # briefing creation and delivery
│   └── telegram_bot.py  # two-way bot
├── models/              # SQLAlchemy: tenant, user, gopos_connection + data tables
├── repositories/
│   ├── store.py         # writes/CRUD/KV, ingestion — always tenant-scoped (+RLS GUC)
│   ├── analytics.py     # all analytics (profit, food cost, "Today", menu engineering…)
│   └── agent_repo.py    # agent-facing facade (routes to store/analytics)
├── pos/                 # POS abstraction: base + gopos + factory (per tenant)
├── data/sync.py         # ingestion through provider (backfill/incremental/all tenants)
├── agents/orchestra.py  # 5 AI agents + tools (tool calling)
├── api/                 # endpoints: auth, onboarding, dashboard, chat, recipes, sync, webhooks
├── migrations/          # Alembic (initial → pos_type → RLS)
├── static/              # design-system.css (Direction C), manifest.json, sw.js, icons
└── templates/           # login, onboarding, onboarding_gopos, dashboard (Alpine.js SPA)
```

**Multi-tenancy (two layers):**
1. Manual filtering by `tenant_id` in repositories — `tenant_id` always comes from the
   JWT, never from user input.
2. **Row-Level Security** on Postgres — `SET app.current_tenant` on the connection; the
   database blocks access to other tenants’ rows even if a code-level filter is missed.

---

## Key features

- **Live “Today” view** — daily break-even point, sales vs. target, end-of-day projection
  based on the hourly curve, and pace vs. the same day last week.
- **Actual net profit** — revenue − food cost − labor costs (from schedules) − fixed costs;
  any date range, including historical months.
- **Menu & food cost** — recipe coverage, menu engineering (stars/plow horses/puzzles/dogs),
  and an “attention required” panel.
- **Audit + plan** — CEO interview → profile → research → 90-day plan → goals with
  automatically updated progress.
- **Five agents** — CEO, Financial, Menu, Growth, and Operations (tool calling with real data).
- **Morning briefing** — via Telegram/Slack, every morning.
- **PWA** — installable on a phone and runs full-screen.

---

## Security and isolation
- Passwords: **bcrypt**. Sessions: **JWT** in an `httpOnly` cookie (set `COOKIE_SECURE=true`
  when using HTTPS).
- GoPOS credentials: encrypted at rest with **Fernet** (they are never displayed again).
- GoPOS: **read-only** mode (`GOPOS_READ_ONLY=true`) — the app never writes to the POS.
- Tenant isolation: `tenant_id` filtering + RLS (Postgres).

---

## CLI commands
```bash
python -m app.seed                       # demo data (Bistro Demo)
alembic upgrade head                     # migrations (schema + RLS on Postgres)
alembic downgrade -1                     # roll back the latest migration
python cli.py db-status                   # data status (first/specified restaurant)
python cli.py sync --hours 24             # manual incremental sync
python cli.py backfill --days 180         # historical backfill
python cli.py ask "What was the profit in May?" # ask the CEO Agent from the terminal
```

---

## Troubleshooting

**`alembic upgrade head` cannot connect to Neon**
Check `DATABASE_URL` (it must include `?sslmode=require`). On some networks, you may need
to use a non-pooled connection (the “direct” endpoint in the Neon dashboard).

**The agent does not respond / LLM error**
Check `OPENAI_API_KEY`. For Grok, also set `OPENAI_BASE_URL=https://api.x.ai/v1`,
`OPENAI_MODEL=grok-3-latest`, and, to import a menu from a photo,
`OPENAI_VISION_MODEL=grok-2-vision-1212`.

**Cannot decrypt GoPOS credentials**
`FERNET_KEY` changed after the credentials were saved. Restore the previous key or reconnect
GoPOS.

**Light/dark mode**
The app follows the system setting; the sidebar toggle overrides the mode (saved in the browser).

**Scheduler/sync does not run in tests**
Set `DISABLE_SCHEDULER=true`.
