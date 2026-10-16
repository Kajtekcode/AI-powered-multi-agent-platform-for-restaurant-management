"""Invoice OCR + payment tracking endpoints (Round B+)."""
import logging
import os
import uuid

from fastapi import APIRouter, Depends, UploadFile, File, HTTPException

from app.api._repos import get_repos, Repos

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/invoices", tags=["invoices"])

UPLOAD_DIR = os.environ.get("INVOICE_UPLOAD_DIR", "data/invoices")
ALLOWED = {"image/jpeg", "image/png", "image/webp", "image/heic", "image/heif",
           "image/gif", "application/pdf", "application/octet-stream"}
MAX_BYTES = 20 * 1024 * 1024  # 20 MB (phone photos can be large; we downscale later)


@router.post("/upload")
async def upload_invoice(file: UploadFile = File(...), repos: Repos = Depends(get_repos)):
    media_type = file.content_type or "image/jpeg"
    # be lenient: accept anything that looks like an image or pdf — we normalize later
    if media_type not in ALLOWED and not media_type.startswith("image/"):
        raise HTTPException(400, "Nieobsługiwany format. Dodaj zdjęcie (JPG/PNG/HEIC) lub PDF.")
    data = await file.read()
    if len(data) > MAX_BYTES:
        raise HTTPException(400, "Plik za duży (max 20 MB).")

    os.makedirs(UPLOAD_DIR, exist_ok=True)
    ext = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp",
           "image/heic": ".heic", "image/heif": ".heif", "application/pdf": ".pdf"}.get(media_type, ".img")
    fname = f"{repos.tenant.id}_{uuid.uuid4().hex}{ext}"
    path = os.path.join(UPLOAD_DIR, fname)
    try:
        with open(path, "wb") as fh:
            fh.write(data)
    except Exception as e:
        logger.warning(f"could not save invoice image: {e}")
        path = None

    from app.core.invoices import extract_and_store
    try:
        invoice = extract_and_store(repos.store, data, media_type, image_url=path)
        return {"status": "parsed", "invoice": invoice}
    except RuntimeError as e:
        # _prepare_image raises user-friendly messages (HEIC/PDF guidance)
        logger.error(f"invoice OCR prep failed: {e}")
        raise HTTPException(422, str(e))
    except Exception as e:
        logger.error(f"invoice OCR failed: {e}")
        raise HTTPException(502, "Nie udało się odczytać faktury. Spróbuj wyraźniejszego zdjęcia "
                                 "lub dodaj dane ręcznie.")


@router.get("")
async def list_invoices(status: str | None = None, repos: Repos = Depends(get_repos)):
    return {"invoices": repos.store.list_invoices(payment_status=status),
            "totals": repos.store.invoice_totals()}


@router.get("/{invoice_id}")
async def get_invoice(invoice_id: str, repos: Repos = Depends(get_repos)):
    inv = repos.store.get_invoice(invoice_id)
    if not inv:
        raise HTTPException(404, "Nie znaleziono faktury.")
    return inv


@router.post("/{invoice_id}/paid")
async def mark_paid(invoice_id: str, paid: bool = True, repos: Repos = Depends(get_repos)):
    if not repos.store.set_invoice_paid(invoice_id, paid):
        raise HTTPException(404, "Nie znaleziono faktury.")
    return {"status": "updated", "paid": paid}


@router.delete("/{invoice_id}")
async def delete_invoice(invoice_id: str, repos: Repos = Depends(get_repos)):
    repos.store.delete_invoice(invoice_id)
    return {"status": "deleted"}


@router.get("/costs/summary")
async def costs_summary(days: int = 30, repos: Repos = Depends(get_repos)):
    """Company costs from invoices + real-vs-assumed food cost verification."""
    summary = repos.store.invoice_costs_summary(days=days)
    # revenue for the same window
    revenue = 0.0
    try:
        sales = repos.analytics.get_sales_summary(days=days)
        revenue = sales.get("total_revenue", 0.0) or 0.0
    except Exception:
        pass
    real_food_cost_pct = round(summary["food_purchases"] / revenue * 100, 1) if revenue else None
    # assumed food cost from recipes
    assumed = None
    try:
        fc = repos.analytics.calculate_food_cost_pct(repos.store, days=days)
        assumed = fc.get("food_cost_pct")
    except Exception:
        pass
    verdict = None
    if real_food_cost_pct is not None and assumed is not None:
        diff = round(real_food_cost_pct - assumed, 1)
        verdict = {"diff": diff,
                   "note": ("Realne zakupy zgadzają się z recepturami." if abs(diff) <= 3 else
                            ("Realny food cost jest WYŻSZY niż zakładany — sprawdź porcje, straty lub ceny."
                             if diff > 0 else
                             "Realny food cost jest niższy niż zakładany — być może masz zapas lub receptury są zawyżone."))}
    return {"summary": summary, "revenue": round(revenue, 2),
            "real_food_cost_pct": real_food_cost_pct, "assumed_food_cost_pct": assumed,
            "verdict": verdict}
