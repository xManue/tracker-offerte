import re
import unicodedata

from .store import latest, record

SEED = (
    {"id": "rtx4070-asus-dual", "name": "ASUS Dual GeForce RTX 4070 12 GB", "category": "GPU",
     "brand": "ASUS", "mpn": "DUAL-RTX4070-12G", "aliases": ["asus dual rtx 4070", "asus dual geforce rtx 4070"],
     "attrs": {"chip": "RTX 4070", "memory_gb": 12, "gpu_mm": 267, "min_psu_w": 650}},
    {"id": "rtx4070-msi-ventus", "name": "MSI Ventus 2X RTX 4070 12 GB", "category": "GPU",
     "brand": "MSI", "mpn": "RTX4070-VENTUS-2X-12G", "aliases": ["msi ventus 2x rtx 4070", "rtx 4070 ventus 2x"],
     "attrs": {"chip": "RTX 4070", "memory_gb": 12, "gpu_mm": 242, "min_psu_w": 650}},
    {"id": "rtx4070-super", "name": "ASUS Dual GeForce RTX 4070 SUPER 12 GB", "category": "GPU",
     "brand": "ASUS", "mpn": "DUAL-RTX4070S-12G", "aliases": ["asus dual rtx 4070 super", "asus dual geforce rtx 4070 super"],
     "attrs": {"chip": "RTX 4070 SUPER", "memory_gb": 12, "gpu_mm": 267, "min_psu_w": 650}},
    {"id": "ryzen5600", "name": "AMD Ryzen 5 5600", "category": "CPU", "brand": "AMD", "mpn": "100-100000927BOX",
     "aliases": ["ryzen 5 5600"], "attrs": {"socket": "AM4"}},
    {"id": "kit-am4", "name": "Kit B550 mATX + 16 GB DDR4", "category": "Kit", "brand": "",
     "aliases": ["kit b550 matx 16 gb ddr4"],
     "attrs": {"socket": "AM4", "ram": "DDR4", "ram_installed": "DDR4", "form_factor": "mATX"}},
    {"id": "case-650", "name": "Case mATX con alimentatore 650 W", "category": "Kit", "brand": "",
     "aliases": ["case matx alimentatore 650 w", "case matx con alimentatore 650 w"],
     "attrs": {"form_factors": ["mATX", "ITX"], "max_gpu_mm": 310, "psu_w": 650}},
    {"id": "pc-am4-4070", "name": "PC Ryzen 5600 + RTX 4070", "category": "PC", "brand": "",
     "aliases": ["pc ryzen 5600 rtx 4070"], "attrs": {}},
)


def clean(value):
    return " ".join(re.findall(r"[a-z0-9]+", unicodedata.normalize("NFKD", value).lower()))


def catalog(session, mode, cutoff=None):
    return {row.external_id: row.data for row in latest(session, mode, "product", cutoff=cutoff)}


def seed_catalog(session, mode, known=None):
    for product in SEED:
        record(session, mode, "product", "local", product["id"], product, effective=known, known=known)


def classify(title, description=""):
    text = clean(title + " " + description)
    if re.search(r"\b(scatola vuota|solo scatola|imballo vuoto|empty box)\b", text):
        return "packaging"
    if re.search(r"\b(ventola|dissipatore|backplate|waterblock|ricambio|replacement fan)\b", clean(title)):
        return "part"
    if re.search(r"\b(guasto|guasta|non funzionante|for parts|difettoso)\b", text):
        return "broken"
    if re.search(r"\b(pc gaming|pc completo|desktop gaming|lotto)\b", clean(title)):
        return "lot"
    if re.search(r"\b(cavo|supporto|adattatore)\b", clean(title)):
        return "accessory"
    return "single"


def match(offer, products):
    title, description = offer.get("title", ""), offer.get("description", "")
    kind = classify(title, description)
    if kind in {"part", "accessory", "packaging", "broken"}:
        return {"product_id": None, "type": kind, "candidates": []}
    text = clean(f"{title} {description} {offer.get('ocr', '')}")
    candidates = []
    for key, product in products.items():
        if (kind == "lot" and product["category"] != "PC") or (kind != "lot" and product["category"] == "PC" and "pc" not in text.split()):
            continue
        reasons, conflicts, score = [], [], 0
        for field in ("gtin", "mpn"):
            code = clean(str(product.get(field) or ""))
            if code and (code == clean(str(offer.get(field) or "")) or f" {code} " in f" {text} "):
                reasons.append(field.upper())
                score = 100
        if any(f" {clean(alias)} " in f" {text} " for alias in product.get("aliases", [])):
            reasons.append("alias completo")
            score = max(score, 83)
        attrs = product.get("attrs", {})
        chip = clean(attrs.get("chip", ""))
        observed_chip = re.search(r"\brtx \d{4}(?: ti super| ti| super)?\b", text)
        if observed_chip and chip:
            if chip != observed_chip.group():
                conflicts.append("chip diverso")
            else:
                reasons.append("chip")
                score = max(score, 43)
        memory = re.search(r"\b(\d+)\s*gb\b", text)
        if chip and memory and attrs.get("memory_gb") != int(memory.group(1)):
            conflicts.append("memoria diversa")
        brand = clean(product["brand"])
        if brand and brand in text.split() and score:
            score = min(100, score + 8)
            reasons.append("marca")
        if score or conflicts:
            candidates.append(dict(id=key, score=score, reasons=reasons, conflicts=conflicts))
    candidates.sort(key=lambda item: (bool(item["conflicts"]), -item["score"], item["id"]))
    eligible = [item for item in candidates if not item["conflicts"] and item["score"] >= 80]
    identified = eligible[0]["id"] if len(eligible) == 1 or (
        len(eligible) > 1 and eligible[0]["score"] >= eligible[1]["score"] + 10
    ) else None
    return {"product_id": identified, "type": kind, "candidates": candidates}
