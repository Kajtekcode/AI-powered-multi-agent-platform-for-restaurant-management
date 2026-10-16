"""Fast 'today' briefing for the advisor home page.

Unlike generate_daily_briefing (5 agents, slow), this is built to load instantly:
it reads already-computed today/30-day data, derives a few signals, and asks the
LLM for ONE short advisor message + up to 3 concrete actions. The result is
cached per day so repeated home-page visits don't re-spend tokens. Falls back to
a deterministic template when the LLM isn't configured.
"""
from __future__ import annotations

import json
import logging
from datetime import date
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)


def _signals(today: Dict, kpis: Dict, net: Optional[Dict]) -> List[str]:
    """Plain-language signals the advisor should weave in / act on."""
    s = []
    be = today.get("breakeven") or {}
    vs = be.get("vs_breakeven")
    if vs is not None:
        if vs >= 0:
            s.append(f"Dziś już nad progiem rentowności o {round(vs)} zł.")
        else:
            s.append(f"Do progu rentowności brakuje dziś {round(abs(vs))} zł.")
    proj = today.get("projection") or {}
    if proj.get("projected_revenue"):
        s.append(f"Prognoza utargu na koniec dnia: ~{round(proj['projected_revenue'])} zł.")
    pace = today.get("pace") or {}
    if pace.get("pct") is not None:
        d = pace["pct"]
        s.append(f"Tempo {'+' if d>=0 else ''}{round(d)}% vs ten sam dzień tydzień temu.")
    if kpis.get("food_cost_pct") is not None and kpis["food_cost_pct"] > 35:
        s.append(f"Food cost {kpis['food_cost_pct']}% — powyżej zdrowego progu (~30-33%).")
    if net and net.get("complete") is False and net.get("basis_note"):
        s.append("Zysk netto liczony bez kosztów pracy (brak RCP) — traktuj jako orientacyjny.")
    return s


def build_today_briefing(repo, restaurant_name: str = "", force: bool = False) -> Dict:
    """Returns {greeting, message, actions[], signals[], generated_at}. Cached per day."""
    cache_key = f"briefing_today:{date.today().isoformat()}"
    if not force:
        cached = repo.get_setting(cache_key)
        if cached:
            try:
                return json.loads(cached)
            except Exception:
                pass

    today = repo.get_today_live() or {}
    kpis = {}
    try:
        kpis = repo.get_sales_summary(days=30) or {}
    except Exception:
        pass
    net = None
    try:
        net = repo.calculate_real_net_profit(days=30)
    except Exception:
        pass
    fc = {}
    try:
        fc = repo.calculate_food_cost_pct(days=30) or {}
    except Exception:
        pass
    kpis["food_cost_pct"] = fc.get("food_cost_pct")

    signals = _signals(today, kpis, net)

    result = _ai_briefing(restaurant_name, today, kpis, net, signals)
    result["signals"] = signals
    result["generated_at"] = date.today().isoformat()

    try:
        repo.set_setting(cache_key, json.dumps(result, ensure_ascii=False))
    except Exception as e:
        logger.warning(f"could not cache briefing: {e}")
    return result


def _ai_briefing(name: str, today: Dict, kpis: Dict, net: Optional[Dict], signals: List[str]) -> Dict:
    from app.core.config import settings
    greeting = _greeting(name)

    if not settings.llm_configured or not signals:
        # Deterministic fallback — still useful, advisor-toned.
        msg = " ".join(signals) if signals else (
            "Dzień dobry! Gdy napłyną dzisiejsze zamówienia, podsumuję dla Ciebie sytuację. "
            "Na razie zerknij na kluczowe wskaźniki poniżej albo zapytaj mnie o cokolwiek.")
        return {"greeting": greeting, "message": msg, "actions": _fallback_actions(today, kpis)}

    data_block = (
        f"Restauracja: {name}\n"
        f"Dziś — utarg: {today.get('revenue')} zł, zamówienia: {today.get('order_count')}, "
        f"do/nad progiem: {(today.get('breakeven') or {}).get('vs_breakeven')} zł\n"
        f"30 dni — przychód: {kpis.get('total_revenue')}, food cost: {kpis.get('food_cost_pct')}%\n"
        f"Sygnały: {'; '.join(signals)}"
    )
    system = (
        "Jesteś doświadczonym, ciepłym doradcą restauracji (jak zaufany dyrektor operacyjny). "
        "Mówisz po polsku, zwięźle, konkretnie i po ludzku — bez korpo-żargonu. "
        "Na podstawie danych napisz krótki poranny briefing (2-3 zdania): co najważniejsze dziś. "
        "Potem zaproponuj 2-3 KONKRETNE działania. Każde działanie MUSI mieć przypisany typ, "
        "który prowadzi użytkownika do odpowiedniego miejsca w aplikacji:\n"
        "- \"chat\": gdy działanie wymaga analizy/rozmowy z doradcą (param = pytanie do zadania)\n"
        "- \"tab\": gdy wystarczy przejść do sekcji (param = jedna z: menu, revenue, operations, "
        "research, goals, recipes)\n"
        "- \"goal\": gdy proponujesz dodać mierzalny cel (param = krótka nazwa celu)\n"
        "Zwróć WYŁĄCZNIE JSON (bez markdown):\n"
        "{\"message\": \"...\", \"actions\": [{\"label\": \"krótkie polecenie\", \"type\": \"chat|tab|goal\", "
        "\"param\": \"pytanie / nazwa sekcji / nazwa celu\"}]}"
    )
    try:
        from openai import OpenAI
        client = OpenAI(api_key=settings.openai_api_key, base_url=settings.openai_base_url)
        r = client.chat.completions.create(
            model=settings.openai_model,
            messages=[{"role": "system", "content": system},
                      {"role": "user", "content": data_block}],
            temperature=0.5, max_tokens=500,
        )
        raw = (r.choices[0].message.content or "").strip()
        raw = raw.replace("```json", "").replace("```", "").strip()
        parsed = json.loads(raw)
        return {"greeting": greeting,
                "message": parsed.get("message", "").strip(),
                "actions": _norm_actions(parsed.get("actions", []))}
    except Exception as e:
        logger.warning(f"AI briefing failed, using fallback: {e}")
        return {"greeting": greeting, "message": " ".join(signals),
                "actions": _fallback_actions(today, kpis)}


def generate_goal_plan(repo, goal: Dict) -> List[str]:
    """Advisor proposes 3-5 concrete, restaurant-specific steps to reach a goal.
    Uses the tenant profile + the goal; LLM with a deterministic fallback."""
    from app.core.config import settings
    metric_pl = {"avg_ticket": "średni rachunek", "revenue": "przychód",
                 "net_margin_pct": "marża netto %", "food_cost_pct": "food cost %",
                 "labor_pct": "koszty pracy %"}.get(goal.get("metric"), goal.get("metric"))
    title = goal.get("title", "")
    target = goal.get("target_value")
    current = goal.get("current_value")

    if not settings.llm_configured:
        return [
            f"Określ, co najbardziej wpływa na „{metric_pl}” w Twoim menu.",
            "Wybierz 2-3 dania/działania o największym wpływie i wdroż je w tym tygodniu.",
            "Sprawdzaj postęp co tydzień w tej zakładce i koryguj.",
        ]
    profile = {}
    try:
        profile = repo.get_profile()
    except Exception:
        pass
    ctx = "\n".join(f"- {k}: {v}" for k, v in profile.items())
    system = (
        "Jesteś praktycznym doradcą restauracji. Zaproponuj 3-5 KONKRETNYCH, wykonalnych "
        "kroków, które realnie przybliżą właściciela do celu. Każdy krok to jedno krótkie "
        "zdanie rozpoczynające się od czasownika (np. Dodaj, Przetestuj, Przeszkol). "
        "Odnoś się do realiów tej restauracji. Zwróć WYŁĄCZNIE JSON: {\"steps\": [\"...\", ...]}."
    )
    user = (f"Cel: {title}\nMetryka: {metric_pl}\nWartość obecna: {current}\nCel docelowy: {target}\n\n"
            f"Kontekst restauracji:\n{ctx}")
    try:
        from openai import OpenAI
        client = OpenAI(api_key=settings.openai_api_key, base_url=settings.openai_base_url)
        r = client.chat.completions.create(
            model=settings.openai_model,
            messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
            temperature=0.5, max_tokens=400,
        )
        raw = (r.choices[0].message.content or "").replace("```json", "").replace("```", "").strip()
        steps = json.loads(raw).get("steps", [])
        return [str(s).strip()[:200] for s in steps if str(s).strip()][:5]
    except Exception as e:
        logger.warning(f"goal plan failed: {e}")
        return [f"Zidentyfikuj 2-3 dźwignie dla „{metric_pl}” i wdroż je w tym tygodniu."]


def _greeting(name: str) -> str:
    from datetime import datetime
    h = datetime.now().hour
    part = "Dzień dobry" if h < 12 else ("Dzień dobry" if h < 18 else "Dobry wieczór")
    return f"{part}{(', ' + name) if name else ''}"


def _norm_actions(actions) -> List[Dict]:
    """Validate/clean AI-proposed actions into {label, type, param}.
    Unknown types/tabs degrade to a safe 'chat' action so a click always does something."""
    valid_tabs = {"menu", "revenue", "operations", "research", "goals", "recipes", "overview", "today"}
    out = []
    for a in (actions or []):
        if not isinstance(a, dict):
            # tolerate a plain string → make it a chat prompt
            if isinstance(a, str) and a.strip():
                out.append({"label": a.strip()[:120], "type": "chat", "param": a.strip()})
            continue
        label = str(a.get("label", "")).strip()[:120]
        if not label:
            continue
        atype = a.get("type", "chat")
        param = str(a.get("param", "")).strip()
        if atype == "tab":
            if param not in valid_tabs:
                atype, param = "chat", label
        elif atype == "goal":
            param = param[:120] or label
        else:
            atype = "chat"
            param = param or label
        out.append({"label": label, "type": atype, "param": param})
    return out[:3]


def _fallback_actions(today: Dict, kpis: Dict) -> List[Dict]:
    acts = []
    if kpis.get("food_cost_pct") and kpis["food_cost_pct"] > 35:
        acts.append({"label": "Sprawdź dania o najwyższym food cost", "type": "tab", "param": "menu"})
    be = (today.get("breakeven") or {})
    if be.get("vs_breakeven") is not None and be["vs_breakeven"] < 0:
        acts.append({"label": "Zaplanuj akcję na wieczór, by dobić do progu",
                     "type": "chat", "param": "Zaproponuj akcję na dziś wieczór, żeby dobić do progu rentowności."})
    if not acts:
        acts.append({"label": "Zapytaj doradcę o dowolny temat",
                     "type": "chat", "param": "Co dziś najbardziej poprawiłoby mój wynik?"})
    return acts[:3]
