import re
import unicodedata

from .store import latest, record

PHONE_CONFLICTS = ("icloud bloccato", "bloccato icloud", "blocco attivazione attivo", "blocco mdm",
                   "imei bloccato", "per ricambi", "solo ricambi", "da riparare")
ACCESSORIES = r"(?:cavo|supporto|adattatore|cover|custodia|pellicola|vetro temperato|caricatore)"

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

EXPANSION = (
    {"id": "rtx3060-12", "name": "GeForce RTX 3060 12 GB", "category": "GPU", "brand": "NVIDIA",
     "aliases": ["rtx 3060 12 gb", "rtx 3060 12gb", "rtx 3060 12g"], "search_query": "RTX 3060 12 GB",
     "attrs": {"chip": "RTX 3060", "memory_gb": 12}},
    {"id": "rtx3070", "name": "GeForce RTX 3070", "category": "GPU", "brand": "NVIDIA",
     "aliases": ["rtx 3070"], "attrs": {"chip": "RTX 3070", "memory_gb": 8}},
    {"id": "rtx4060", "name": "GeForce RTX 4060", "category": "GPU", "brand": "NVIDIA",
     "aliases": ["rtx 4060"], "attrs": {"chip": "RTX 4060", "memory_gb": 8}},
    {"id": "rtx5070", "name": "GeForce RTX 5070", "category": "GPU", "brand": "NVIDIA",
     "aliases": ["rtx 5070"], "attrs": {"chip": "RTX 5070", "memory_gb": 12}},
    {"id": "rx6700xt", "name": "Radeon RX 6700 XT", "category": "GPU", "brand": "AMD",
     "aliases": ["rx 6700 xt", "rx 6700xt"], "attrs": {"chip": "RX 6700 XT", "memory_gb": 12}},
    {"id": "rx7800xt", "name": "Radeon RX 7800 XT", "category": "GPU", "brand": "AMD",
     "aliases": ["rx 7800 xt", "rx 7800xt"], "attrs": {"chip": "RX 7800 XT", "memory_gb": 16}},
    {"id": "ryzen5700x3d", "name": "AMD Ryzen 7 5700X3D", "category": "CPU", "brand": "AMD",
     "aliases": ["ryzen 7 5700x3d", "ryzen 5700x3d"], "attrs": {"socket": "AM4"}},
    {"id": "ps5-slim-disc", "name": "PlayStation 5 Slim con lettore", "category": "Console", "brand": "Sony",
     "aliases": ["ps5 slim con lettore", "playstation 5 slim con lettore", "ps5 slim disc"],
     "conflict_terms": ["digital edition", "senza lettore", "versione digitale"], "attrs": {"drive": "disc"}},
    {"id": "xbox-series-x", "name": "Xbox Series X con lettore", "category": "Console", "brand": "Microsoft",
     "aliases": ["xbox series x con lettore", "xbox series x disc", "xbox series x nero"],
     "conflict_terms": ["digital edition", "senza lettore", "versione digitale"], "attrs": {"drive": "disc"}},
    {"id": "switch-oled", "name": "Nintendo Switch OLED", "category": "Console", "brand": "Nintendo",
     "aliases": ["nintendo switch oled", "switch oled"], "conflict_terms": ["switch lite"],
     "attrs": {"edition": "OLED"}},
    {"id": "lego-75375", "name": "LEGO 75375 Millennium Falcon", "category": "LEGO", "brand": "LEGO",
     "mpn": "75375", "aliases": ["lego 75375"], "attrs": {"set_number": "75375"}},
    {"id": "makita-dhp484", "name": "Makita DHP484", "category": "Utensili", "brand": "Makita",
     "mpn": "DHP484", "aliases": ["makita dhp484", "makita dhp484z"], "attrs": {"kit": "corpo macchina"}},
    {"id": "iphone-15-128", "name": "Apple iPhone 15 128 GB", "category": "Smartphone", "brand": "Apple",
     "aliases": ["iphone 15"], "search_query": "iPhone 15 128GB",
     "conflict_terms": ["iphone 15 pro", "iphone 15 plus", *PHONE_CONFLICTS], "attrs": {"storage_gb": 128}},
    {"id": "iphone-16-128", "name": "Apple iPhone 16 128 GB", "category": "Smartphone", "brand": "Apple",
     "aliases": ["iphone 16"], "search_query": "iPhone 16 128GB",
     "conflict_terms": ["iphone 16 pro", "iphone 16 plus", "iphone 16e", "iphone 16 e", *PHONE_CONFLICTS],
     "attrs": {"storage_gb": 128}},
    {"id": "galaxy-a56-128", "name": "Samsung Galaxy A56 5G 128 GB", "category": "Smartphone", "brand": "Samsung",
     "aliases": ["galaxy a56", "samsung a56"], "search_query": "Samsung Galaxy A56 5G 128GB",
     "conflict_terms": ["enterprise edition", *PHONE_CONFLICTS], "attrs": {"storage_gb": 128}},
    {"id": "switch-2-standard", "name": "Nintendo Switch 2 solo console (BEE-001)", "category": "Console",
     "brand": "Nintendo", "mpn": "BEE-001", "aliases": ["nintendo switch 2", "switch 2"],
     "search_query": "Nintendo Switch 2 console standard",
     "conflict_terms": ["bee 001 01", "batteria rimovibile", "batteria removibile", "batteria sostituibile",
                        "mod 2026", "switch 2 v2", "switch 2 lite", "switch 2 oled", "switch 2 modello oled",
                        "switch 2 edizione oled", "bundle", "solo dock", "solo tablet", "senza dock", "senza joy con",
                        "senza joycon", "dock non inclusa", "joy con non inclusi",
                        "mario kart world", "pokopia", "joy con 2 per", "versione giapponese", "hk version"],
     "attrs": {"edition": "standard", "capacity_gb": 256}},
    {"id": "ps5-slim-digital-825", "name": "PlayStation 5 Slim Digital 825 GB", "category": "Console", "brand": "Sony",
     "aliases": ["ps5 slim digital", "ps5 slim digitale", "ps5 slim edizione digitale", "ps5 slim senza lettore",
                 "playstation 5 slim digital", "playstation 5 slim edizione digitale", "playstation 5 edizione digitale slim"],
     "search_query": "PS5 Slim Digital 825GB",
     "conflict_terms": ["con lettore", "con disco", "bundle", "senza controller", "controller non incluso"],
     "attrs": {"drive": "digital", "storage_gb": 825}},
    {"id": "dualsense-white", "name": "Sony DualSense standard bianco", "category": "Controller", "brand": "Sony",
     "aliases": ["dualsense bianco", "dualsense white", "dualsense wireless bianco", "dualsense wireless white",
                 "dualsense wireless controller white", "dualsense controller wireless bluetooth bianco"],
     "search_query": "Sony DualSense controller bianco originale",
     "conflict_terms": ["dualsense edge", "non originale", "replica", "ps5 slim", "playstation 5 slim", "bundle",
                        "edizione limitata", "stick drift", "hall effect", "tmr", "scocca"],
     "attrs": {"color": "white", "edition": "standard"}},
)


def clean(value):
    return " ".join(re.findall(r"[a-z0-9]+", unicodedata.normalize("NFKD", value).lower()))


def catalog(session, mode, cutoff=None):
    return {row.external_id: row.data for row in latest(session, mode, "product", cutoff=cutoff)}


def seed_catalog(session, mode, known=None, *, include_expansion=True):
    existing = catalog(session, mode)
    products = SEED + EXPANSION if include_expansion else SEED
    for product in products:
        if product["id"] not in existing:
            record(session, mode, "product", "local", product["id"], product, effective=known, known=known)


def classify(title, description=""):
    text = clean(title + " " + description)
    name = clean(title)
    if re.search(r"\b(scatola vuota|solo scatola|imballo vuoto|empty box)\b", text):
        return "packaging"
    if (re.search(r"\b(ventola|dissipatore|backplate|waterblock|ricambio|replacement fan)\b", name)
            or re.match(r"^(?:solo )?(?:display|schermo|batteria)\b", name)):
        return "part"
    if re.search(r"\b(guasto|guasta|non funzionante|for parts|difettoso)\b", text):
        return "broken"
    if re.search(r"\b(pc gaming|pc completo|desktop gaming|lotto)\b", name):
        return "lot"
    alone = re.sub(rf"\b(?:con {ACCESSORIES}|{ACCESSORIES} inclus[oaie])\b", "", name)
    if re.search(rf"\b{ACCESSORIES}\b", alone):
        return "accessory"
    return "single"


def match(offer, products):
    title, description = offer.get("title", ""), offer.get("description", "")
    kind = classify(title, description)
    if kind in {"part", "accessory", "packaging", "broken"}:
        return {"product_id": None, "type": kind, "candidates": []}
    text = clean(f"{title} {description} {offer.get('ocr', '')}")
    text = re.sub(r"\b(rx \d{4})(xtx|xt)\b", r"\1 \2", text)
    text = re.sub(r"\bswitch modello oled\b", "switch oled", text)
    storage = {int(size) * (1024 if unit == "tb" else 1)
               for size, unit in re.findall(r"\b(\d{1,4})\s*(gb|tb)\b", text)
               if unit == "tb" or int(size) >= 64}
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
        if (expected := attrs.get("storage_gb")) and score:
            if (storage and storage != {expected}) or (not storage and score < 100):
                conflicts.append("capacità diversa o non identificata")
        chip = clean(attrs.get("chip", ""))
        observed_chip = re.search(r"\b(?:rtx \d{4}(?: ti super| ti| super)?|rx \d{4}(?: xtx| xt| gre)?)\b", text)
        if observed_chip and chip:
            if chip != observed_chip.group():
                conflicts.append("chip diverso")
            else:
                reasons.append("chip")
                score = max(score, 43)
        memory = re.search(r"\b(\d+)\s*gb\b", text)
        if chip and memory and attrs.get("memory_gb") != int(memory.group(1)):
            conflicts.append("memoria diversa")
        conflicts.extend(f"variante incompatibile ({term})" for term in product.get("conflict_terms", ())
                         if f" {clean(term)} " in f" {text} ")
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
