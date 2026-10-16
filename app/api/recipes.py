"""
Recipes API — CRUD, product catalog lookup, and AI menu-photo extraction.
"""

import base64
import json
import logging
from typing import List, Optional

from fastapi import APIRouter, HTTPException, UploadFile, File, Depends
from pydantic import BaseModel

from app.core.config import settings
from app.api._repos import get_repos, Repos

logger = logging.getLogger(__name__)
router = APIRouter()


class IngredientModel(BaseModel):
    ingredient: str
    quantity: float
    unit: str  # g, kg, szt, portion, ml, l
    cost_per_kg: float


class RecipeModel(BaseModel):
    dish_name: str
    ingredients: List[IngredientModel]
    notes: Optional[str] = ""
    selling_price: Optional[float] = None   # gross PLN from menu / GoPOS
    vat_rate: Optional[float] = None        # 8 or 23 (Poland), or custom


class OverheadModel(BaseModel):
    category: str
    monthly_amount: float
    notes: Optional[str] = ""


# --------------------------------------------------------------------------
# Product catalog (GoPOS items with price + VAT) — for autocomplete in modal
# --------------------------------------------------------------------------

@router.get("/products")
async def list_products(search: Optional[str] = None, limit: int = 200, repos: Repos = Depends(get_repos)):
    """Full GoPOS product catalog with selling prices and VAT rates."""
    return repos.analytics.get_product_catalog(search=search, limit=limit)


# --------------------------------------------------------------------------
# AI Menu extraction from photo
# --------------------------------------------------------------------------

EXTRACT_PROMPT = """Jesteś ekspertem gastronomii. Na zdjęciu jest menu restauracji.
Wyodrębnij WSZYSTKIE dania i napoje widoczne na zdjęciu.

Dla każdego dania zaproponuj realistyczną recepturę (składniki z ilościami) na JEDNĄ porcję,
bazując na nazwie i opisie dania. Użyj typowych polskich cen hurtowych składników (PLN/kg).

Zwróć WYŁĄCZNIE czysty JSON (bez markdown, bez ```), W JEDNEJ LINII, bez wcięć i zbędnych spacji
(kompaktowy format oszczędza miejsce — menu może mieć wiele dań). Format:
{"dishes":[{"dish_name":"nazwa dania dokładnie jak w menu","selling_price":45.0,"vat_rate":8,"category":"przystawki","ingredients":[{"ingredient":"Łosoś świeży","quantity":150,"unit":"g","cost_per_kg":65.0},{"ingredient":"Limonka","quantity":1,"unit":"szt","cost_per_kg":1.5}]}]}

Zasady:
- selling_price: cena ze zdjęcia menu (jeśli widoczna, inaczej null)
- vat_rate: 8 dla jedzenia, 23 dla alkoholu i napojów gazowanych (Polska)
- unit: tylko "g", "kg", "ml", "l", "szt", "portion"
- cost_per_kg: dla "szt"/"portion" podaj cenę za sztukę
- Receptury są PROPOZYCJĄ do weryfikacji przez użytkownika — bądź realistyczny"""


def _parse_dishes(raw: str) -> tuple:
    """
    Parse the AI response into a dishes list.
    Returns (dishes, truncated). If the JSON was cut off mid-stream
    (token limit), salvages every COMPLETE dish object and flags truncated=True.
    """
    cleaned = raw.strip()
    if cleaned.startswith("```"):
        parts = cleaned.split("```")
        if len(parts) > 1:
            cleaned = parts[1]
        if cleaned.lstrip().lower().startswith("json"):
            cleaned = cleaned.lstrip()[4:]
    cleaned = cleaned.strip()

    # Happy path — valid JSON
    try:
        data = json.loads(cleaned)
        if isinstance(data, list):
            return data, False
        return data.get("dishes", []), False
    except json.JSONDecodeError:
        pass

    # Salvage path — response truncated by token limit.
    # Walk the dishes array and decode complete objects one by one.
    idx = cleaned.find('"dishes"')
    arr_start = cleaned.find('[', idx) if idx != -1 else cleaned.find('[')
    if arr_start == -1:
        return [], True

    decoder = json.JSONDecoder()
    dishes = []
    i = arr_start + 1
    n = len(cleaned)
    while i < n:
        while i < n and cleaned[i] in ' \t\r\n,':
            i += 1
        if i >= n or cleaned[i] == ']':
            break
        try:
            obj, end = decoder.raw_decode(cleaned, i)
        except json.JSONDecodeError:
            break  # hit the truncated tail — keep what we have
        if isinstance(obj, dict) and obj.get("dish_name"):
            dishes.append(obj)
        i = end
    return dishes, True


@router.post("/extract-menu")
async def extract_menu_from_photo(file: UploadFile = File(...), repos: Repos = Depends(get_repos)):
    """
    Upload a photo of a restaurant menu. AI (vision) extracts all dishes
    with prices, VAT guesses, and draft recipes for the user to review.
    """
    if not settings.llm_configured:
        raise HTTPException(status_code=400, detail="Brak klucza API modelu — ustaw OPENAI_API_KEY w .env")

    content = await file.read()
    if len(content) > 15 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="Plik za duży (max 15 MB)")

    mime = file.content_type or "image/jpeg"
    if not mime.startswith("image/"):
        raise HTTPException(status_code=400, detail="Prześlij zdjęcie (JPG/PNG)")

    b64 = base64.b64encode(content).decode()

    from openai import OpenAI
    client = OpenAI(api_key=settings.openai_api_key, base_url=settings.openai_base_url)

    # Vision model: dedicated env var, fallback to main model
    import os
    vision_model = os.getenv("OPENAI_VISION_MODEL", settings.openai_model)

    def _call(max_tok: int):
        return client.chat.completions.create(
            model=vision_model,
            messages=[{
                "role": "user",
                "content": [
                    {"type": "text", "text": EXTRACT_PROMPT},
                    {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}},
                ],
            }],
            max_tokens=max_tok,
            temperature=0.2,
        )

    try:
        try:
            response = _call(8000)
        except Exception as e:
            # Some models cap output length — retry with a safe limit
            if "max_tokens" in str(e).lower():
                response = _call(4096)
            else:
                raise
        raw = response.choices[0].message.content or ""
        finish_reason = response.choices[0].finish_reason
    except Exception as e:
        msg = str(e)
        if "model" in msg.lower() and ("vision" in msg.lower() or "image" in msg.lower() or "not support" in msg.lower()):
            raise HTTPException(
                status_code=400,
                detail=f"Model '{vision_model}' nie obsługuje obrazów. Dla Grok ustaw w .env: "
                       f"OPENAI_VISION_MODEL=grok-2-vision-1212",
            )
        raise HTTPException(status_code=500, detail=f"Błąd modelu AI: {msg[:200]}")

    dishes, truncated = _parse_dishes(raw)
    if not dishes:
        logger.error(f"Menu extraction unparseable: {raw[:300]}")
        raise HTTPException(
            status_code=500,
            detail="Nie udało się odczytać dań z odpowiedzi AI — spróbuj ponownie lub z wyraźniejszym zdjęciem",
        )

    # Enrich with GoPOS catalog match (real price + VAT beats AI guess)
    for d in dishes:
        matches = repos.analytics.search_menu(d.get("dish_name", ""), limit=1)
        if matches and matches[0].get("menu_price"):
            d["gopos_match"] = matches[0]["item_name"]
            d["selling_price"] = matches[0]["menu_price"]
            if matches[0].get("vat_rate") is not None:
                d["vat_rate"] = matches[0]["vat_rate"]

    result = {"dishes": dishes, "count": len(dishes), "model": vision_model}
    if truncated or finish_reason == "length":
        result["note"] = (
            f"Menu było długie — odpowiedź AI została ucięta, ale udało się wyodrębnić "
            f"{len(dishes)} kompletnych dań. Brakujące pozycje dodasz wgrywając zdjęcie "
            f"fragmentami (np. osobno każdą stronę/sekcję menu)."
        )
    return result


# --------------------------------------------------------------------------
# Recipe CRUD
# --------------------------------------------------------------------------

@router.get("/")
async def list_recipes(repos: Repos = Depends(get_repos)):
    """All recipes with computed food cost, margin and margin% per dish."""
    return repos.analytics.get_all_recipes_with_costs(repos.store)


@router.get("/{dish_name}")
async def get_recipe(dish_name: str, repos: Repos = Depends(get_repos)):
    recipe = repos.store.get_recipe(dish_name)
    if not recipe:
        raise HTTPException(status_code=404, detail=f"Recipe '{dish_name}' not found")
    return recipe


@router.post("/")
async def save_recipe(recipe: RecipeModel, repos: Repos = Depends(get_repos)):
    repos.store.save_recipe(
        dish_name=recipe.dish_name,
        ingredients=[i.model_dump() for i in recipe.ingredients],
        notes=recipe.notes or "",
        selling_price=recipe.selling_price,
        vat_rate=recipe.vat_rate,
    )
    return {"status": "saved", "dish_name": recipe.dish_name}


@router.delete("/{dish_name}")
async def delete_recipe(dish_name: str, repos: Repos = Depends(get_repos)):
    repos.store.delete_recipe(dish_name)
    return {"status": "deleted", "dish_name": dish_name}


@router.get("/{dish_name}/cost")
async def get_dish_cost(dish_name: str, repos: Repos = Depends(get_repos)):
    cost = repos.analytics.calculate_dish_food_cost(repos.store, dish_name)
    if not cost:
        raise HTTPException(status_code=404, detail=f"Recipe '{dish_name}' not found or has no ingredients")
    return cost


# --------------------------------------------------------------------------
# Overheads
# --------------------------------------------------------------------------

@router.get("/overheads/list")
async def list_overheads(repos: Repos = Depends(get_repos)):
    return repos.store.get_overhead_costs()


@router.post("/overheads/")
async def save_overhead(overhead: OverheadModel, repos: Repos = Depends(get_repos)):
    repos.store.set_overhead_cost(overhead.category, overhead.monthly_amount, overhead.notes or "")
    return {"status": "saved", "category": overhead.category}


@router.delete("/overheads/{category}")
async def delete_overhead(category: str, repos: Repos = Depends(get_repos)):
    repos.store.delete_overhead_cost(category)
    return {"status": "deleted", "category": category}
