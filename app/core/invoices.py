"""Invoice/receipt OCR + extraction (Round B+).

A photo or PDF of a purchase invoice is sent to the vision model, which returns
structured data: supplier, dates, payment terms, line items with prices. We then
store the invoice, its items, and feed ingredient prices into the price-history
table (so 'masło podrożało o 18%' becomes answerable).

The vision call is injectable (extract_fn) so the parsing/storage pipeline is
fully testable against a mock without a real model or network.
"""
from __future__ import annotations

import base64
import json
import logging
from datetime import datetime
from typing import Callable, Dict, Optional

logger = logging.getLogger(__name__)

EXTRACTION_PROMPT = (
    "Jesteś systemem do odczytu polskich faktur i paragonów zakupowych restauracji. "
    "Przeanalizuj obraz i zwróć WYŁĄCZNIE poprawny JSON (bez markdown, bez ```), w formacie:\n"
    "{\n"
    '  "supplier": "nazwa sprzedawcy",\n'
    '  "invoice_number": "numer faktury lub null (często zaczyna się od FV, np. FV/2025/04/095)",\n'
    '  "invoice_date": "DD.MM.YYYY (Data wystawienia / sprzedaży)",\n'
    '  "due_date": "DD.MM.YYYY (Termin płatności / Płatne do; jeśli \'Płatność X dni\' → invoice_date + X dni; jeśli brak → invoice_date + 7 dni)",\n'
    '  "payment_method": "cash|card|transfer",\n'
    '  "payment_status": "paid|unpaid (paragon/gotówka/karta = paid; faktura z terminem = unpaid)",\n'
    '  "currency": "PLN",\n'
    '  "total": liczba (kwota brutto całej faktury),\n'
    '  "total_net": liczba lub null,\n'
    '  "category": "JEDZENIE|NAPOJE|WINO|NAPOJE ALKOHOLOWE|CHEMIA|MEDIA|INNE (dominująca kategoria pozycji)",\n'
    '  "items": [\n'
    "    {\n"
    '      "name": "nazwa produktu (np. Ostrygi Bretania N2)",\n'
    '      "unit": "jednostka BAZOWA do której sprowadzasz cenę: szt|kg|l",\n'
    '      "pack_size": liczba jednostek bazowych w opakowaniu zbiorczym (np. karton 36 ostryg → 36; zgrzewka 6×1,5l → 9 (litry); worek 2,5 kg → 2.5 (kg)); jeśli sprzedawane luzem na szt/kg/l → 1,\n'
    '      "pack_label": "krótki opis opakowania jeśli dotyczy (np. \'karton 36 szt\') lub null",\n'
    '      "net_price": cena NETTO za JEDNĄ jednostkę BAZOWĄ (1 szt / 1 kg / 1 l) — jeśli cena z faktury dotyczy opakowania, PODZIEL ją przez pack_size,\n'
    '      "vat_percent": stawka VAT w % (np. 5, 8, 23),\n'
    '      "unit_price": cena BRUTTO za jednostkę bazową (net_price + net_price*vat_percent/100),\n'
    '      "total_price": wartość pozycji brutto lub null,\n'
    '      "category": "JEDZENIE|NAPOJE|WINO|NAPOJE ALKOHOLOWE|CHEMIA|MEDIA|INNE"\n'
    "    }\n"
    "  ]\n"
    "}\n"
    "ZASADY:\n"
    "- CENA ZA JEDNOSTKĘ BAZOWĄ (najważniejsze): zawsze sprowadzaj cenę do JEDNEJ sztuki / 1 kg / 1 l, żeby ceny od różnych dostawców były porównywalne. "
    "Jeśli produkt jest w opakowaniu zbiorczym, a liczbę sztuk/wagę widać na fakturze albo w nazwie (np. 'Ostrygi karton 36 szt' za 36 zł → net_price=1,00 zł/szt; 'Woda 6×1,5 l' za 18 zł → net_price=2,00 zł/l), policz net_price = cena_opakowania / pack_size i ustaw unit na jednostkę bazową (szt/kg/l).\n"
    "- IGNORUJ kupioną ilość pozycji (np. 2 kartony) i wartości sumaryczne — interesuje nas cena jednostkowa za 1 szt/kg/l.\n"
    "- Kwoty jako liczby z kropką dziesiętną, bez walut w polach liczbowych.\n"
    "- Kategorie pozycji: JEDZENIE (produkty spożywcze: mięso, ryby, owoce morza, warzywa, mąka, sery), "
    "NAPOJE (bezalkoholowe: woda, soki, lemoniada, kawa, herbata), "
    "WINO (wszystkie wina: czerwone, białe, różowe, musujące, prosecco, szampan — także w BIB/bag-in-box), "
    "NAPOJE ALKOHOLOWE (alkohol INNY niż wino: piwo, wódka, whisky, gin, likiery, drinki), "
    "CHEMIA (środki czystości, detergenty, worki, rękawiczki), MEDIA (prąd/energia elektryczna, gaz, woda z wodociągów, "
    "internet, telefon, wywóz śmieci, czynsz), INNE (opakowania, usługi, pozostałe).\n"
    "- Kategoria całej faktury = dominująca kategoria pozycji.\n"
    "- Pomijaj tylko rabaty i pozycje czysto rozliczeniowe. Pozycje typu 'energia elektryczna', 'opłata abonamentowa', "
    "'opłata handlowa' za media ZACHOWAJ (kategoria MEDIA).\n"
    "- Nie zgaduj — jeśli pola nie ma na obrazie, użyj null. Przy niepewnych pozycjach pomiń je, "
    "ale zachowaj poprawną strukturę JSON."
)

# Map Polish invoice categories → internal cost buckets used for food-cost math.
CATEGORY_MAP = {
    "JEDZENIE": "food", "NAPOJE": "drinks", "WINO": "wine", "NAPOJE ALKOHOLOWE": "alcohol",
    "CHEMIA": "chemistry", "MEDIA": "media", "INNE": "other",
}


MAX_PDF_PAGES = 8  # cap so a huge multi-page PDF doesn't blow up the vision payload


def _normalize_jpeg(image_bytes: bytes) -> bytes:
    """One raster image → clean RGB JPEG (≤2200px). Handles HEIC/PNG/WEBP via Pillow."""
    import io
    from PIL import Image
    try:
        import pillow_heif  # type: ignore
        pillow_heif.register_heif_opener()
    except Exception:
        pass
    try:
        img = Image.open(io.BytesIO(image_bytes))
        img.load()
    except Exception as e:
        raise RuntimeError(
            "Nie udało się odczytać obrazu. Jeśli to zdjęcie z iPhone'a (HEIC), "
            "ustaw w aparacie format „Najbardziej zgodny” (JPEG) lub zrób zrzut ekranu. "
            f"({e})"
        )
    if img.mode != "RGB":
        img = img.convert("RGB")
    max_dim = 2200
    if max(img.size) > max_dim:
        ratio = max_dim / max(img.size)
        img = img.resize((int(img.size[0] * ratio), int(img.size[1] * ratio)))
    out = io.BytesIO()
    img.save(out, format="JPEG", quality=85)
    return out.getvalue()


def _prepare_images(image_bytes: bytes, media_type: str) -> list:
    """Convert ANY uploaded image/PDF into a LIST of clean JPEGs the vision model accepts —
    one per page for PDFs (so multi-page invoices are read in full), one for a single image.
    Handles iPhone HEIC, PNG/WEBP and oversized photos. Raises with a clear message on failure."""
    # PDF → render EVERY page to an image (needs PyMuPDF)
    if media_type == "application/pdf" or image_bytes[:5] == b"%PDF-":
        try:
            import fitz  # PyMuPDF
            doc = fitz.open(stream=image_bytes, filetype="pdf")
            pages = []
            for i in range(min(doc.page_count, MAX_PDF_PAGES)):
                pix = doc.load_page(i).get_pixmap(dpi=170)
                pages.append(_normalize_jpeg(pix.tobytes("png")))
            if pages:
                return pages
        except Exception as e:
            raise RuntimeError(
                "Nie potrafię odczytać PDF — zrób zdjęcie faktury (JPG/PNG) "
                f"albo zainstaluj PyMuPDF. ({e})"
            )
    # single raster image
    return [_normalize_jpeg(image_bytes)]


def _prepare_image(image_bytes: bytes, media_type: str) -> bytes:
    """Back-compat: first page only."""
    return _prepare_images(image_bytes, media_type)[0]


def _default_extract(image_bytes: bytes, media_type: str) -> Dict:
    """Real vision extraction via the configured vision model (OpenAI-compatible).
    Sends ALL pages of a multi-page invoice so items from every page are merged."""
    from app.core.config import settings
    if not settings.llm_configured:
        raise RuntimeError("LLM not configured — cannot run OCR")
    from openai import OpenAI

    jpegs = _prepare_images(image_bytes, media_type)
    prompt = EXTRACTION_PROMPT
    if len(jpegs) > 1:
        prompt += (f"\n\nUWAGA: ta faktura ma {len(jpegs)} STRON (kolejne obrazy poniżej). "
                   "Połącz pozycje ze WSZYSTKICH stron w JEDEN JSON; sumy i numer faktury "
                   "weź z całości dokumentu (zwykle z ostatniej strony).")
    content = [{"type": "text", "text": prompt}]
    for j in jpegs:
        b64 = base64.b64encode(j).decode("ascii")
        content.append({"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}})

    client = OpenAI(api_key=settings.openai_api_key, base_url=settings.openai_base_url)
    model = settings.openai_vision_model or settings.openai_model
    resp = client.chat.completions.create(
        model=model,
        messages=[{"role": "user", "content": content}],
        temperature=0.1, max_tokens=2400,
    )
    raw = (resp.choices[0].message.content or "").replace("```json", "").replace("```", "").strip()
    return json.loads(raw)


def _parse_date(v) -> Optional[datetime]:
    if not v or not isinstance(v, str):
        return None
    for fmt in ("%Y-%m-%d", "%d.%m.%Y", "%d/%m/%Y", "%Y/%m/%d"):
        try:
            return datetime.strptime(v.strip(), fmt)
        except ValueError:
            continue
    return None


def _num(v) -> Optional[float]:
    try:
        if isinstance(v, str):
            v = v.replace(" ", "").replace(",", ".")
        return float(v)
    except (TypeError, ValueError):
        return None


def extract_and_store(repo, image_bytes: bytes, media_type: str,
                      image_url: Optional[str] = None,
                      extract_fn: Optional[Callable] = None) -> Dict:
    """Run OCR extraction, then persist the invoice + items + price history.
    Returns the created invoice dict. Raises on extraction failure (caller handles)."""
    extract = extract_fn or _default_extract
    data = extract(image_bytes, media_type)

    supplier = (data.get("supplier") or "").strip() or None
    pay_method = data.get("payment_method")
    pay_status = data.get("payment_status") or ("paid" if pay_method in ("cash", "card") else "unpaid")
    if pay_status not in ("paid", "unpaid"):
        pay_status = "unpaid"
    inv_date = _parse_date(data.get("invoice_date"))
    due_date = _parse_date(data.get("due_date"))
    # If a transfer invoice has no due date, default to invoice_date + 7 days (Polish norm)
    if pay_status == "unpaid" and not due_date and inv_date:
        from datetime import timedelta
        due_date = inv_date + timedelta(days=7)
    paid_at = inv_date if pay_status == "paid" else None
    category = CATEGORY_MAP.get((data.get("category") or "").upper().strip(), data.get("category") or "other")

    invoice = repo.create_invoice(
        supplier=supplier,
        invoice_number=(data.get("invoice_number") or None),
        invoice_date=inv_date,
        due_date=due_date,
        total=_num(data.get("total")),
        total_net=_num(data.get("total_net")),
        currency=(data.get("currency") or "PLN"),
        payment_method=pay_method,
        payment_status=pay_status,
        paid_at=paid_at,
        category=category,
        image_url=image_url,
        ocr_raw=json.dumps(data, ensure_ascii=False),
        status="parsed",
    )

    items = data.get("items") or []
    for it in items:
        name = (it.get("name") or "").strip()
        if not name:
            continue
        unit = it.get("unit")
        net_price = _num(it.get("net_price"))
        unit_price = _num(it.get("unit_price"))
        # derive gross from net+vat if gross missing
        vat = _num(it.get("vat_percent"))
        if unit_price is None and net_price is not None and vat is not None:
            unit_price = round(net_price * (1 + vat / 100), 2)
        item_cat = CATEGORY_MAP.get((it.get("category") or "").upper().strip(),
                                    it.get("category") or category)
        repo.add_invoice_item(
            invoice_id=invoice["id"], raw_name=name,
            quantity=_num(it.get("quantity")), unit=unit,
            unit_price=unit_price, total_price=_num(it.get("total_price")),
            net_price=net_price, vat_percent=vat, category=item_cat,
        )
        # feed the golden price-history table with the comparable NET per-unit price
        price_for_history = net_price if net_price is not None else unit_price
        if price_for_history is not None:
            repo.record_ingredient_price(
                ingredient=name, price=price_for_history, unit=unit, supplier=supplier,
                source="invoice", recorded_at=inv_date or datetime.utcnow(),
                category=item_cat,
            )

    invoice["item_count"] = len([i for i in items if (i.get("name") or "").strip()])
    return invoice
