from datetime import timedelta

from .catalog import SEED, seed_catalog
from .channels import set_source
from .db import now
from .ingest import preview, write_rows
from .operations import transition_inventory
from .store import events, record

GPU, GPU_ALT, GPU_SUPER, CPU, KIT, CASE, PC = [row["name"] for row in SEED]


def seed(session):
    mode = "demo"
    if events(session, mode, "seed"):
        return False
    clock = now()
    seed_catalog(session, mode, clock - timedelta(days=200))
    for source in ("ebay", "subito", "vinted", "amazon", "aliexpress", "local"):
        set_source(session, mode, source, history=True, training=source in {"ebay", "subito"},
                   note="Dati sintetici")
    offers, sales = [], []

    def listed(identifier, title, price, shipping, *, days=0, condition="used", source="subito", **rest):
        offers.append((source, {"external_id": identifier, "title": title, "price": price, "shipping": shipping,
                               "status": "available", "quantity": 1, "condition": condition,
                               "observed_at": (clock - timedelta(days=days, minutes=3)).isoformat(), **rest}))

    def sold(identifier, title, price, days, *, source="ebay", **rest):
        sales.append((source, {"external_id": identifier, "title": title, "quantity": 1, "condition": "used",
                              "status": "sold", "paid_price": price, "verified": True, "sale_format": "fixed",
                              "sold_at": (clock - timedelta(days=days)).isoformat(),
                              "observed_at": (clock - timedelta(days=days, hours=-3)).isoformat(),
                             "decision_at": (clock - timedelta(days=days + 10)).isoformat(),
                             "decision_title": title, "decision_condition": "used", "decision_format": "fixed", **rest}))

    for i, price in enumerate((390, 410, 415, 420, 449)):
        sold(f"gpu-{i}", GPU, price, 3 + i * 7)
        sold(f"subito-gpu-{i}", GPU, price - 50, 5 + i * 6, source="subito")
    for name, title, base in (("cpu", CPU, 95), ("kit", KIT, 115), ("case", CASE, 78), ("pc", PC, 495)):
        for i in range(4):
            sold(f"{name}-{i}", title, base + i * 5, 4 + i * 9)
    sold("best-offer", GPU, None, 1, shown_price=575)
    for index in range(10):
        identifier = f"followed-{index}"
        initial = clock - timedelta(days=80 + index)
        listed(identifier, GPU, 400 + index, 0, days=80 + index, source="ebay",
               published_at=(initial - timedelta(hours=1)).isoformat())
        sold(f"followed-sale-{index}", GPU, 398 + index, 70 + index, listing_id=identifier)
        listed(identifier, GPU, 400 + index, 0, days=69 + index, source="ebay", status="unavailable")
    listed("gpu-1", GPU, 281, 9, days=40, mpn="DUAL-RTX4070-12G")
    listed("gpu-1", GPU, 250, 9, mpn="DUAL-RTX4070-12G", photo_hash="c3a7f11523bd8c10", area="Bologna")
    listed("gpu-reposted", GPU, 257, 8, source="vinted", photo_hash="c3a7f11523bd8c12", area="Bologna")
    listed("pc-gaming", "PC gaming vecchio", 370, 12, description="Scheda RTX 4070, processore Ryzen 5 5600")
    listed("ricambio", "Ventola ricambio ASUS Dual RTX 4070", 15, 3, source="aliexpress", condition="new")
    listed("super", GPU_SUPER, 315, 7, source="ebay")
    listed("used-import", GPU_ALT, 305, None, source="amazon", status="unknown")
    listed("cpu", CPU, 55, 5, source="ebay")
    listed("kit", KIT, 55, 6)
    listed("case", CASE, 38, 0)
    for kind, rows in (("sale", sales), ("offer", offers)):
        for source in sorted({source for source, _ in rows}):
            local = [(i, item) for i, (key, item) in enumerate(rows, 1) if key == source]
            valid, errors = preview(local, {}, kind)
            if errors:
                raise RuntimeError(str(errors))
            write_rows(session, mode, source, kind, valid, errors, f"demo-{kind}-{source}")
    for source, listing in (("subito", "gpu-1"), ("vinted", "gpu-reposted")):
        record(session, mode, "duplicate", "local", f"{source}:{listing}", {"group": "demo-gpu-bologna"})
    record(session, mode, "lot", "subito", "pc-gaming",
           {"title": "PC gaming vecchio", "whole_product_id": SEED[6]["id"], "components": [
               {"product_id": item["id"], "quantity": 1, "identification": "confirmed", "condition": "used",
                "evidence": "Specifiche della demo confermate manualmente"}
               for item in (SEED[0], SEED[3], SEED[4], SEED[5])]})
    stock = [(1, {"external_id": "a", "title": GPU, "quantity": 8, "condition": "used"}),
             (2, {"external_id": "b", "title": "Schede da verificare", "condition": "unknown"})]
    valid, errors = preview(stock, {}, "stock")
    write_rows(session, mode, "local", "stock", valid, errors, "demo-stock")
    record(session, mode, "request", "local", "demo-request",
           {"product_id": SEED[0]["id"], "budget": "280", "quantity": 1, "conditions": ["used"], "area": "",
            "expires": (clock + timedelta(days=30)).date().isoformat(), "opt_in": False})
    transition_inventory(session, mode, "demo-inventory", "purchased",
                         {"title": GPU_ALT, "purchase": "260.00", "specs": "RTX 4070 · 12 GB · testata",
                          "group": "demo-purchased-other", "product_id": SEED[1]["id"]})
    transition_inventory(session, mode, "demo-inventory", "verified", {})
    record(session, mode, "draft", "local", "demo-draft",
           {"item": "demo-inventory", "channel": "ebay", "title": GPU_ALT, "price": "399.00",
            "description": "RTX 4070 con 12 GB. Scheda testata.", "currency": "EUR",
            "publisher": "https://www.ebay.it/sl/sell", "state": "draft"})
    record(session, mode, "seed", "local", "v1", {"synthetic": True})
    return True
