"""Market & competitor research engine (Round B).

Hybrid approach: a pluggable search provider gathers raw web results across
several areas (competitors, our reputation, local events, food/ingredient
trends, market context), then the LLM (Grok) synthesises them into a structured
Polish report with concrete implications and suggested goals.

Designed to be testable: the search provider and the LLM call are both
injectable, so the whole pipeline runs against mocks with no network.

Progress is written to sync_state so the UI can poll it:
    research_status : 'running' | 'done' | 'error'
    research_label  : human label of the current phase
    research_report : the final markdown (also mirrored to setting 'research_report')
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Callable, Dict, List, Optional

logger = logging.getLogger(__name__)

# Research areas the user asked for (full scope).
AREAS = [
    "competitors",      # menu, ceny, opinie konkurencji
    "reputation",       # nasze opinie online (Google/Facebook)
    "local_events",     # wydarzenia lokalne wpływające na ruch
    "food_trends",      # trendy gastronomiczne i ceny składników
    "market",           # szerszy kontekst rynkowy do ustawiania celów
]


def build_queries(profile: Dict[str, str], restaurant_name: Optional[str],
                  city: Optional[str]) -> Dict[str, List[str]]:
    """Turn the tenant's audit profile into targeted search queries per area.
    Pure function — easy to unit-test."""
    name = (restaurant_name or profile.get("restaurant_name") or "").strip()
    city = (city or _city_from(profile) or "").strip()
    concept = (profile.get("concept") or "").strip()
    competitors = _split_competitors(profile.get("competitors", ""))
    cuisine_hint = _cuisine_hint(concept)

    q: Dict[str, List[str]] = {a: [] for a in AREAS}

    # Competitors — named ones + generic local rivals
    for comp in competitors[:3]:
        q["competitors"].append(f"{comp} {city} menu opinie")
    q["competitors"].append(f"najlepsze restauracje {cuisine_hint} {city}".strip())
    q["competitors"].append(f"restauracje {city} ceny menu {cuisine_hint}".strip())

    # Our reputation
    if name:
        q["reputation"].append(f"{name} {city} opinie")
        q["reputation"].append(f"{name} {city} Google reviews")

    # Local events / footfall drivers
    q["local_events"].append(f"wydarzenia {city} 2026 kalendarz imprez")
    q["local_events"].append(f"{city} festiwale koncerty turystyka 2026")

    # Food trends + ingredient prices
    q["food_trends"].append("trendy gastronomiczne restauracje 2026 Polska")
    q["food_trends"].append("ceny produktów spożywczych gastronomia 2026 wzrost")
    if cuisine_hint:
        q["food_trends"].append(f"trendy {cuisine_hint} restauracje 2026")

    # Market context for goal-setting
    q["market"].append(f"rynek gastronomiczny {city} 2026 raport")
    q["market"].append("branża restauracyjna Polska 2026 marże koszty trendy")

    # Drop empties
    return {a: [s for s in lst if s and s.strip()] for a, lst in q.items()}


def _split_competitors(raw: str) -> List[str]:
    if not raw:
        return []
    for sep in [",", ";", "/", " i ", "\n"]:
        raw = raw.replace(sep, "|")
    return [c.strip() for c in raw.split("|") if c.strip()]


def _city_from(profile: Dict[str, str]) -> str:
    # location_context often holds the city in free text; take a best-effort token
    loc = profile.get("location_context", "") or profile.get("city", "")
    return loc.split(",")[0].strip() if loc else ""


def _cuisine_hint(concept: str) -> str:
    c = (concept or "").lower()
    table = {
        "owoc": "owoce morza", "ryb": "rybna", "sea": "owoce morza",
        "włos": "włoska", "piz": "pizzeria", "burg": "burgery",
        "sushi": "sushi", "azjat": "azjatycka", "wege": "wegańska",
        "stek": "stekowa", "wołow": "stekowa",
    }
    for k, v in table.items():
        if k in c:
            return v
    return ""


class ResearchService:
    """Runs the research pipeline for one tenant.

    search_fn(query, max_results) -> list[{title,url,snippet}]
    llm_fn(system, user) -> str   (synthesis)
    Both are injectable for testing; defaults wire to the real provider + Grok.
    """

    def __init__(self, repo, search_fn: Optional[Callable] = None,
                 llm_fn: Optional[Callable] = None,
                 restaurant_name: Optional[str] = None, city: Optional[str] = None):
        self.repo = repo
        self.restaurant_name = restaurant_name
        self.city = city
        self._search = search_fn or self._default_search
        self._llm = llm_fn or self._default_llm

    # ---- default backends (real) -------------------------------------- #
    def _default_search(self, query: str, max_results: int = 5):
        from app.core.research.providers import build_search_provider
        return build_search_provider().search(query, max_results=max_results)

    def _default_llm(self, system: str, user: str) -> str:
        from app.core.config import settings
        if not settings.llm_configured:
            return ""
        from openai import OpenAI
        client = OpenAI(api_key=settings.openai_api_key, base_url=settings.openai_base_url)
        r = client.chat.completions.create(
            model=settings.openai_model,
            messages=[{"role": "system", "content": system},
                      {"role": "user", "content": user}],
            temperature=0.4, max_tokens=2800,
        )
        return (r.choices[0].message.content or "").strip()

    # ---- pipeline ------------------------------------------------------ #
    def run(self, max_per_query: int = 4) -> Dict:
        repo = self.repo
        repo.set_sync_state("research_status", "running")
        try:
            profile = repo.get_profile()
            queries = build_queries(profile, self.restaurant_name, self.city)

            gathered: Dict[str, List[Dict]] = {}
            for area in AREAS:
                repo.set_sync_state("research_label", f"szukam: {_area_pl(area)}")
                results: List[Dict] = []
                seen = set()
                for query in queries.get(area, []):
                    for hit in self._search(query, max_per_query):
                        url = hit.get("url", "")
                        if url and url not in seen:
                            seen.add(url)
                            results.append(hit)
                gathered[area] = results[:8]

            repo.set_sync_state("research_label", "analizuję wyniki (AI)…")
            report, suggested_goals = self._synthesize(profile, gathered)

            repo.set_setting("research_report", report)
            repo.set_setting("research_report_date", datetime.now(timezone.utc).isoformat())
            repo.set_setting("research_suggested_goals", json.dumps(suggested_goals, ensure_ascii=False))
            repo.set_sync_state("research_status", "done")
            repo.set_sync_state("research_label", "")
            total_hits = sum(len(v) for v in gathered.values())
            logger.info(f"research done: {total_hits} sources, {len(suggested_goals)} suggested goals")
            return {"status": "done", "sources": total_hits, "report": report,
                    "suggested_goals": suggested_goals}
        except Exception as e:
            logger.error(f"research failed: {e}")
            repo.set_sync_state("research_status", "error")
            return {"status": "error", "message": str(e)}

    def _synthesize(self, profile: Dict[str, str], gathered: Dict[str, List[Dict]]):
        """Returns (report_markdown, suggested_goals: list[dict]).
        The report is INDEPENDENT research — analysing what was found online,
        not repeating the owner's audit answers."""
        blocks = []
        for area in AREAS:
            hits = gathered.get(area, [])
            if not hits:
                continue
            lines = [f"### {_area_pl(area)}"]
            for h in hits:
                lines.append(f"- {h.get('title','')} | {h.get('snippet','')} | {h.get('url','')}")
            blocks.append("\n".join(lines))
        raw = "\n\n".join(blocks) if blocks else "(brak wyników wyszukiwania)"

        # Profile is CONTEXT ONLY — who we are. Not material to repeat back.
        ctx = "\n".join(f"- {k}: {v}" for k, v in profile.items())

        system = (
            "Jesteś analitykiem rynku gastronomicznego w Polsce. Twoim zadaniem jest "
            "NIEZALEŻNA analiza rynku na podstawie WYNIKÓW WYSZUKIWANIA z internetu — NIE "
            "streszczaj tego, co właściciel powiedział o sobie. Profil restauracji służy Ci "
            "tylko do kontekstu (kim są, gdzie są). Skup się na tym, co wynika z zewnętrznych "
            "źródeł: czego dowiadujesz się o konkurencji, opiniach, wydarzeniach, trendach i "
            "cenach. Wyciągaj wnioski, których właściciel SAM o sobie nie napisał. "
            "Pisz po polsku, konkretnie, z odniesieniem do źródeł. Jeśli danych brak — powiedz "
            "to wprost i podaj realistyczne założenie branżowe, ale nie zmyślaj nazw ani liczb."
        )
        user = f"""KONTEKST (kim jest ta restauracja — NIE powtarzaj tego, użyj tylko do osadzenia analizy):
{ctx}

WYNIKI WYSZUKIWANIA Z INTERNETU (to jest Twój materiał do analizy):
{raw}

Napisz raport w markdown. Zasady:
- Pierwsza osoba liczby mnogiej tylko gdy mówisz o rekomendacjach; analiza ma być obiektywna.
- W każdej sekcji odnoś się do KONKRETÓW z wyników (nazwy, ceny, opinie, daty wydarzeń).
- NIE przepisuj profilu właściciela. Jeśli źródło potwierdza/przeczy czemuś z profilu — to ciekawe, napisz o tym.

Sekcje:
## Konkurencja
Kto realnie konkuruje (z wyników), ich ceny/pozycjonowanie, gdzie jest luka rynkowa dla nas.
## Reputacja online
Co mówią opinie (nasze i konkurencji), powracające tematy, co to znaczy.
## Wydarzenia i ruch
Konkretne nadchodzące wydarzenia/sezony z wyników i jak je zmonetyzować.
## Trendy i koszty
Trendy gastronomiczne 2026 i presja kosztowa (składniki) — z wyników.
## Wnioski i rekomendacje
3-5 konkretnych ruchów, każdy z uzasadnieniem z analizy powyżej.

Na samym KOŃCU dodaj blok z propozycjami mierzalnych celów w formacie JSON (dokładnie taki blok, nic po nim):
```goals
[
  {{"title": "krótki opis celu", "metric": "avg_ticket|revenue|net_margin_pct|food_cost_pct|labor_pct", "target_value": liczba, "direction": "increase|decrease", "rationale": "dlaczego, 1 zdanie z analizy"}}
]
```
Zaproponuj 3-5 celów wynikających z ANALIZY (nie z profilu). target_value to liczba (zł lub %)."""

        out = self._llm(system, user)
        if not out:
            return ("# Analiza rynku\n\n_Synteza AI niedostępna (brak konfiguracji LLM). "
                    "Poniżej zebrane źródła do ręcznego przeglądu._\n\n" + raw, [])

        report, goals = _extract_goals(out)
        return report, goals


def _extract_goals(text: str):
    """Pull the ```goals JSON block out of the report. Returns (clean_report, goals)."""
    import re
    goals = []
    m = re.search(r"```goals\s*(.*?)```", text, re.DOTALL)
    if m:
        try:
            parsed = json.loads(m.group(1).strip())
            if isinstance(parsed, list):
                valid_metrics = {"avg_ticket", "revenue", "net_margin_pct", "food_cost_pct", "labor_pct"}
                for g in parsed:
                    if not isinstance(g, dict):
                        continue
                    metric = g.get("metric", "manual")
                    if metric not in valid_metrics:
                        metric = "manual"
                    try:
                        tv = float(g.get("target_value"))
                    except (TypeError, ValueError):
                        continue
                    goals.append({
                        "title": str(g.get("title", "")).strip()[:200] or "Cel",
                        "metric": metric, "target_value": tv,
                        "direction": "decrease" if g.get("direction") == "decrease" else "increase",
                        "rationale": str(g.get("rationale", "")).strip()[:300],
                    })
        except Exception as e:
            logger.warning(f"could not parse suggested goals: {e}")
    # remove the goals block from the displayed report
    clean = re.sub(r"```goals\s*.*?```", "", text, flags=re.DOTALL).strip()
    return clean, goals


def _area_pl(area: str) -> str:
    return {
        "competitors": "Konkurencja",
        "reputation": "Nasza reputacja online",
        "local_events": "Wydarzenia lokalne",
        "food_trends": "Trendy i ceny składników",
        "market": "Kontekst rynkowy",
    }.get(area, area)


def run_research_for_tenant(tenant_id: str) -> Dict:
    """Entry point for background execution — self-contained (own scope+session)."""
    from app.core.db import db_session
    from app.core.tenant_context import tenant_scope
    from app.repositories.agent_repo import AgentRepo
    from app.models.tenant import Tenant, OnboardingStep, step_rank

    with tenant_scope(tenant_id), db_session() as db:
        repo = AgentRepo(db, tenant_id)
        tenant = db.query(Tenant).filter_by(id=tenant_id).first()
        svc = ResearchService(
            repo,
            restaurant_name=tenant.name if tenant else None,
        )
        result = svc.run()
        # advance onboarding (research done) if we got a report
        if result.get("status") == "done" and tenant:
            try:
                if step_rank(tenant.onboarding_step) < step_rank(OnboardingStep.research_done):
                    tenant.onboarding_step = OnboardingStep.research_done
                    db.commit()
            except Exception as e:
                logger.warning(f"could not advance onboarding after research: {e}")
        return result
