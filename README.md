# 🍽️ Restaurant Growth OS

**Platforma SaaS dla restauracji — Twój zespół menedżerów AI.** Pięciu wyspecjalizowanych
agentów analizuje dane z kasy GoPOS, prowadzi audyt firmy, pilnuje progu rentowności,
liczy realny zysk netto i codziennie rano przygotowuje konkretny plan działania.

Stack: **FastAPI · SQLAlchemy · PostgreSQL (Neon) · Alpine.js + Tailwind · OpenAI/Grok**.
Architektura **multi-tenant** (wiele restauracji, pełna izolacja danych), instalowalna jako **PWA**.

---

## Spis treści
1. [Wymagania](#wymagania)
2. [Szybki start lokalnie (SQLite)](#szybki-start-lokalnie-sqlite)
3. [Uruchomienie na Neon (Postgres)](#uruchomienie-na-neon-postgres)
4. [Test krok po kroku](#test-krok-po-kroku)
5. [Architektura](#architektura)
6. [Najważniejsze funkcje](#najważniejsze-funkcje)
7. [Bezpieczeństwo i izolacja](#bezpieczeństwo-i-izolacja)
8. [Komendy CLI](#komendy-cli)
9. [Rozwiązywanie problemów](#rozwiązywanie-problemów)

---

## Wymagania
- **Python 3.11+** (testowane na 3.11–3.14)
- Konto **Neon** (darmowy Postgres serverless) — lub dowolny Postgres
- Klucz **OpenAI** lub **xAI/Grok** (agenci AI)
- Dane dostępowe **GoPOS API** dla Twojej restauracji (Organization ID, Client ID, Client Secret)

---

## Szybki start lokalnie (SQLite)

Najszybszy sposób, żeby zobaczyć aplikację — bez Postgresa, na wbudowanym SQLite.

```bash
# 1. Środowisko
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# 2. Konfiguracja
cp .env.example .env
#    Minimalnie ustaw w .env:
#      JWT_SECRET=<openssl rand -hex 32>
#      FERNET_KEY=<python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())">
#      OPENAI_API_KEY=... (+ OPENAI_BASE_URL/OPENAI_MODEL jeśli Grok)
#    DATABASE_URL możesz pominąć — użyje sqlite:///data/dev.db

# 3. Schemat bazy
alembic upgrade head

# 4. (opcjonalnie) dane demo — restauracja "Bistro Demo" z 659 zamówieniami
python -m app.seed
#    Login: demo@bistro.pl / demo12345

# 5. Start
python run.py
#    → http://localhost:8000
```

Bez danych demo trafisz na ekran rejestracji i przejdziesz pełny onboarding.

---

## Uruchomienie na Neon (Postgres)

To jest docelowy tryb produkcyjny — z prawdziwą izolacją multi-tenant i RLS.

### 1. Utwórz bazę na Neon
Załóż projekt na [neon.tech](https://neon.tech), skopiuj **connection string** (z `?sslmode=require`).

### 2. Skonfiguruj `.env`
```bash
cp .env.example .env
```
Ustaw w `.env`:
```ini
DATABASE_URL=postgresql://USER:PASSWORD@HOST/DATABASE?sslmode=require
JWT_SECRET=<openssl rand -hex 32>
FERNET_KEY=<python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())">
COOKIE_SECURE=true          # za HTTPS
OPENAI_API_KEY=...           # lub klucz Grok + OPENAI_BASE_URL + OPENAI_MODEL
```

> ⚠️ **FERNET_KEY** wygeneruj **raz** i nie zmieniaj po zapisaniu kluczy GoPOS — inaczej nie odszyfrujesz zapisanych sekretów.
>
> ⚠️ Jeśli używałeś tymczasowego `DATABASE_URL` z hasłem w jawnej formie — **zrotuj hasło** w panelu Neon przed go-live.

### 3. Migracje (tworzą schemat + włączają RLS)
```bash
pip install -r requirements.txt
alembic upgrade head
```
Na Postgresie ostatnia migracja (`c1f5a7e2b9d0`) **realnie włącza Row-Level Security** na wszystkich tabelach z danymi. Na SQLite ta sama migracja jest pomijana (no-op).

### 4. Start
```bash
python run.py --prod
```

---

## Test krok po kroku

Pełna ścieżka, którą warto przejść po wdrożeniu:

1. **Rejestracja** — wejdź na `/`. Bez sesji przekieruje na `/login`. Kliknij „Załóż konto",
   podaj nazwę restauracji, miasto, e-mail i hasło (min. 8 znaków).
2. **Onboarding — krok 2: GoPOS** — kreator przekieruje Cię do ekranu łączenia. Wpisz
   Organization ID, Client ID i Client Secret z panelu GoPOS. Klucze zostaną **zaszyfrowane**
   i zapiszą się w bazie; ruszy pobieranie historii w tle.
3. **Onboarding — krok 3: Audyt** — CEO Agent zada kilkanaście pytań o Twój biznes
   (wymaga skonfigurowanego LLM). Możesz przerwać i wrócić — postęp się zapisuje.
4. **Pozostałe kroki** — analiza rynku, przepisy (możesz dodać ze zdjęcia menu), włączenie
   porannego briefingu. Po ostatnim kroku trafiasz na dashboard.
5. **Dashboard** — sprawdź zakładki: „Dzisiaj" (próg rentowności na żywo), „Przegląd"
   (KPI, wykresy), „Menu" (food cost, menu engineering), „Czat" (rozmowa z agentami).
   W panelu bocznym: nazwa restauracji, e-mail, **Wyloguj**, przełącznik trybu jasny/ciemny.
6. **Wyloguj / zaloguj** — sprawdź, że sesja działa i po wylogowaniu `/` znów wymaga logowania.
7. **PWA** — na telefonie otwórz aplikację w przeglądarce → „Dodaj do ekranu głównego".
   Uruchomi się w trybie pełnoekranowym z własną ikoną.
8. **Izolacja (opcjonalnie)** — załóż drugie konto z inną restauracją. Zobaczysz, że nie
   ma dostępu do danych pierwszej — ani przez aplikację, ani (na Postgresie) na poziomie bazy.

---

## Architektura

```
app/
├── main.py              # FastAPI: routing stron, gating onboardingu, PWA, lifespan
├── core/
│   ├── config.py        # ustawienia (env)
│   ├── db.py            # engine + sesje (Neon/SQLite), init_db
│   ├── security.py      # bcrypt, JWT, Fernet (szyfrowanie kluczy POS)
│   ├── deps.py          # zależności FastAPI: bieżący użytkownik/tenant, gating
│   ├── tenant_context.py# ContextVar bieżącego tenanta
│   ├── scheduler.py     # auto-sync + poranny briefing (po wszystkich tenantach)
│   ├── notifications.py # budowa i wysyłka briefingu
│   └── telegram_bot.py  # dwukierunkowy bot
├── models/              # SQLAlchemy: tenant, user, gopos_connection + tabele danych
├── repositories/
│   ├── store.py         # zapisy/CRUD/KV, ingestia — zawsze zawężone do tenanta (+RLS GUC)
│   ├── analytics.py     # wszystkie analizy (zysk, food cost, "Dzisiaj", menu engineering…)
│   └── agent_repo.py    # fasada dla agentów (routuje do store/analytics)
├── pos/                 # abstrakcja POS: base + gopos + factory (per-tenant)
├── data/sync.py         # ingestion przez providera (backfill/incremental/all-tenants)
├── agents/orchestra.py  # 5 agentów AI + narzędzia (tool calling)
├── api/                 # endpointy: auth, onboarding, dashboard, chat, recipes, sync, webhooks
├── migrations/          # Alembic (initial → pos_type → RLS)
├── static/              # design-system.css (Kierunek C), manifest.json, sw.js, ikony
└── templates/           # login, onboarding, onboarding_gopos, dashboard (Alpine.js SPA)
```

**Multi-tenancy (dwie warstwy):**
1. Ręczne zawężanie po `tenant_id` w repozytoriach — `tenant_id` zawsze z JWT, nigdy z inputu.
2. **Row-Level Security** na Postgresie — `SET app.current_tenant` na połączeniu; baza odcina
   cudze wiersze nawet gdyby filtr w kodzie umknął.

---

## Najważniejsze funkcje

- **„Dzisiaj" na żywo** — próg rentowności dnia, utarg vs próg, projekcja końca dnia z krzywej
  godzinowej, tempo vs ten sam dzień tydzień temu.
- **Realny zysk netto** — przychód − food cost − koszty pracy (z grafików) − koszty stałe;
  dowolny okres, w tym miesiące historyczne.
- **Menu & food cost** — pokrycie recepturami, menu engineering (gwiazdy/konie/zagadki/psy),
  panel „wymaga uwagi".
- **Audyt + plan** — wywiad CEO → profil → research → plan 90-dniowy → cele z auto-postępem.
- **Pięciu agentów** — CEO, Financial, Menu, Growth, Operations (tool calling na realnych danych).
- **Poranny briefing** — Telegram/Slack, codziennie rano.
- **PWA** — instalowalna na telefon, działa pełnoekranowo.

---

## Bezpieczeństwo i izolacja
- Hasła: **bcrypt**. Sesje: **JWT** w cookie `httpOnly` (ustaw `COOKIE_SECURE=true` za HTTPS).
- Klucze GoPOS: szyfrowane **Fernet** at-rest (nie są nigdzie pokazywane ponownie).
- GoPOS: tryb **tylko do odczytu** (`GOPOS_READ_ONLY=true`) — nigdy nie zapisujemy do kasy.
- Izolacja tenantów: filtr `tenant_id` + RLS (Postgres).

---

## Komendy CLI
```bash
python -m app.seed                       # dane demo (Bistro Demo)
alembic upgrade head                     # migracje (schemat + RLS na Postgresie)
alembic downgrade -1                      # cofnij ostatnią migrację
python cli.py db-status                   # status danych (pierwsza/wskazana restauracja)
python cli.py sync --hours 24             # ręczny sync przyrostowy
python cli.py backfill --days 180         # backfill historii
python cli.py ask "Jaki był zysk w maju?" # zapytaj CEO Agenta z terminala
```

---

## Rozwiązywanie problemów

**`alembic upgrade head` nie łączy się z Neon**
Sprawdź `DATABASE_URL` (z `?sslmode=require`). W niektórych sieciach trzeba użyć
połączenia bez poolingu (endpoint „direct" w panelu Neon).

**Agent nie odpowiada / błąd LLM**
Sprawdź `OPENAI_API_KEY`. Dla Grok ustaw też `OPENAI_BASE_URL=https://api.x.ai/v1`,
`OPENAI_MODEL=grok-3-latest`, a do importu menu ze zdjęcia `OPENAI_VISION_MODEL=grok-2-vision-1212`.

**Nie odszyfrowuje kluczy GoPOS**
`FERNET_KEY` zmienił się po zapisaniu danych. Przywróć poprzedni klucz albo połącz GoPOS ponownie.

**Tryb jasny/ciemny**
Aplikacja podąża za ustawieniem systemu; przełącznik w panelu bocznym wymusza tryb (zapis w przeglądarce).

**Scheduler/sync nie działa w testach**
Ustaw `DISABLE_SCHEDULER=true`.
