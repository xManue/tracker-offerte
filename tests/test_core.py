from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from decimal import Decimal

import httpx
from sqlalchemy import select

from tracker.catalog import SEED, catalog, match, seed_catalog
from tracker.channels import set_source
from tracker.dataset import examples
from tracker.db import Event, Job, now
from tracker.demo import GPU, GPU_SUPER, seed
from tracker.ingest import amount, normalize, preview, write_rows
from tracker.operations import component_estimates, offer_cards, plan_budget
from tracker.replay import replay
from tracker.store import enqueue, events, latest, utc
from tracker.value import assess, delivered, margin
from tracker.worker import claim, run


def import_rows(session, kind, source, records, batch="test", mode="demo"):
    rows, errors = preview(list(enumerate(records, 1)), {}, kind)
    assert not errors
    return write_rows(session, mode, source, kind, rows, errors, batch)


def test_decimal_and_unknown_states():
    assert amount("€ 1.243,01") == "1243.01"
    assert amount("1,243.01") == "1243.01"
    assert delivered({"price": "250", "shipping": None}) is None
    converted = delivered({"price": "100", "shipping": "10", "currency": "USD",
                           "fx_rate": "0.90", "fx_source": "fixture", "fx_date": "2026-01-01"})
    assert converted == Decimal(99)
    values = margin(Decimal(250), converted, Decimal(".13"), Decimal(9), Decimal(1))
    assert values["fee"] == "32.50"
    assert values["profit"] == "108.50"
    offer, *_ = normalize({"external_id": "fx-test", "title": GPU, "price": "100", "shipping": "0",
                            "currency": "USD", "fx_rate": "0,90", "fx_source": "fixture",
                            "fx_date": "01/01/2026"}, {}, "offer")
    assert offer["fx_date"] == "2026-01-01"


def test_model_conflicts_and_non_products():
    products = {row["id"]: row for row in SEED}
    assert match({"title": GPU_SUPER}, products)["product_id"] == "rtx4070-super"
    assert match({"title": "ASUS Dual RTX 4070 8 GB"}, products)["product_id"] is None
    assert match({"title": "PC gaming vecchio", "description": "RTX 4070"}, products)["type"] == "lot"
    assert match({"title": "Ventola RTX 4070 ASUS Dual"}, products)["type"] == "part"
    assert match({"title": "RTX 4070 NVIDIA"}, products)["product_id"] is None


def test_history_reimport_and_distinct_order_lines(sessions):
    timestamp = now() - timedelta(days=2)
    offer = {"external_id": "1", "title": GPU, "price": "250", "shipping": "0", "status": "available"}
    sale = {"external_id": "order-ln1", "order_id": "order-1", "listing_id": "1", "title": GPU,
            "quantity": 2, "status": "sold", "verified": True, "paid_price": "650",
            "sold_at": timestamp.isoformat(), "customer_email": "example@example.test", "address": "not stored"}
    with sessions.begin() as session:
        assert import_rows(session, "offer", "subito", [offer], "file-a") == 1
        assert import_rows(session, "offer", "subito", [{**offer, "price": "220"}], "file-b") == 1
        assert import_rows(session, "offer", "subito", [offer], "file-c") == 1
        assert import_rows(session, "offer", "subito", [offer], "file-a") == 0
        assert import_rows(session, "sale", "ebay", [sale, {**sale, "external_id": "order-ln2"}]) == 2
        assert import_rows(session, "sale", "ebay", [sale], "another-file") == 0
        assert [row.data["price"] for row in events(session, "demo", "offer")] == ["250", "220", "250"]
        assert all("customer_email" not in row.data and "address" not in row.data for row in latest(session, "demo", "sale"))


def test_concurrent_claim_and_expired_lease(sessions):
    with sessions.begin() as session:
        for key in range(8):
            enqueue(session, "demo", "evaluate", str(key), {})

    def take(_):
        with sessions.begin() as session:
            return claim(session, "demo").id

    with ThreadPoolExecutor(max_workers=4) as pool:
        claimed = list(pool.map(take, range(8)))
    assert len(set(claimed)) == 8
    with sessions.begin() as session:
        job = session.get(Job, claimed[0])
        old_token = job.lease_token
        job.lease_until = now() - timedelta(seconds=1)
    with sessions.begin() as session:
        retry = claim(session, "demo")
        assert retry.id == claimed[0]
        assert retry.lease_token != old_token and retry.attempts == 2


def test_price_evidence_and_budget_uses_one_physical_item(sessions):
    with sessions.begin() as session:
        seed(session)
        assert not seed(session)
    run(sessions, ("demo",), once=True)
    with sessions() as session:
        cards = offer_cards(session, "demo")
        card = next(card for card in cards if card["offer"].source == "subito" and card["offer"].external_id == "gpu-1")
        assert card["assessment"]["purchase"] == "259.00"
        estimates = {item["channel"]: item for item in card["assessment"]["estimates"]}
        assert {"subito", "ebay"} <= estimates.keys()
        assert estimates["ebay"]["comparables"]["high"] != "575.00"
        assert all(example["unit_eur"] != "575.00" for example in estimates["ebay"]["comparables"]["evidence"])
        plan = plan_budget(session, "demo", cards, Decimal(420), Decimal(20), Decimal(1), 30)
        assert Decimal(plan["spent"]) <= 420
        assert any(item["reason"] == "Stessa unità fisica" for item in plan["excluded"])
        alerts = events(session, "demo", "alert")
        assert any(item.data["snapshot"]["purchase"] == "259.00" for item in alerts)
        assert not list(session.scalars(select(Job).where(Job.status == "failed")))
    run(sessions, ("demo",), once=True)
    with sessions() as session:
        assert len(events(session, "demo", "alert")) == len(alerts)


def test_temporal_dataset_uses_observed_comparables_and_permissions(sessions):
    current = now()
    decision = current - timedelta(days=10)
    with sessions.begin() as session:
        seed_catalog(session, "demo", current - timedelta(days=60))
        set_source(session, "demo", "ebay", history=True, training=True)
        set_source(session, "demo", "subito", history=True, training=False)
        baseline = {"title": GPU, "quantity": 1, "status": "sold", "verified": True, "paid_price": "300",
                    "decision_title": GPU, "condition": "used", "decision_condition": "used",
                    "sale_format": "fixed", "decision_format": "fixed"}
        sold = (decision - timedelta(days=3)).isoformat()
        observed = (decision - timedelta(days=2)).isoformat()
        import_rows(session, "sale", "ebay", [
            {**baseline, "external_id": "known", "sold_at": sold, "observed_at": observed},
            {**baseline, "external_id": "same-order", "order_id": "pair", "sold_at": sold, "observed_at": observed},
            {**baseline, "external_id": "late", "sold_at": sold},
            {**baseline, "external_id": "target", "order_id": "pair", "decision_at": decision.isoformat(),
             "sold_at": (decision + timedelta(days=2)).isoformat()},
        ])
        import_rows(session, "sale", "subito", [
            {**baseline, "external_id": "forbidden", "destination": "ebay", "sold_at": sold, "observed_at": observed},
        ])
    with sessions() as session:
        rows, manifest = examples(session, "demo")
        assert manifest["sources"] == ["ebay"]
        assert len(rows) == 1 and rows[0]["comparison_count"] == 1
        proof = session.get(Event, rows[0]["comparison_event_ids"][0])
        assert proof.external_id == "known" and utc(proof.known_at) <= decision


def test_replay_ignores_future_market_and_uses_checks_at_execution(sessions):
    clock = now()
    purchase_at = clock - timedelta(days=5)
    finish = purchase_at + timedelta(hours=2)
    with sessions.begin() as session:
        seed_catalog(session, "demo", clock - timedelta(days=100))
        sales = [{"external_id": f"price-{i}", "title": GPU, "quantity": 1, "verified": True,
                  "status": "sold", "paid_price": "500", "condition": "used",
                  "sold_at": (purchase_at - timedelta(days=1)).isoformat()} for i in range(3)]
        import_rows(session, "sale", "ebay", sales)
        offer = {"external_id": "focus", "title": GPU, "status": "available", "price": "100",
                 "shipping": "0", "condition": "used", "observed_at": purchase_at.isoformat()}
        import_rows(session, "offer", "subito", [offer])
        outcome = replay(session, "demo", purchase_at, finish, Decimal(200), "margin", 15, True, 14)
        assert outcome["committed"] == "0.00"
        import_rows(session, "sale", "ebay", [{**row, "external_id": f"known-{i}",
            "observed_at": (purchase_at - timedelta(hours=3)).isoformat()} for i, row in enumerate(sales)], "known")
        assert replay(session, "demo", purchase_at, finish, Decimal(200), "margin", 15, True, 14)["committed"] == "100.00"
        assert replay(session, "demo", purchase_at, finish, Decimal(200), "margin", 15, False, 14)["committed"] == "0.00"
        import_rows(session, "offer", "subito", [{**offer, "status": "unknown",
                    "observed_at": (purchase_at + timedelta(minutes=15)).isoformat()}], "failed-check")
        assert replay(session, "demo", purchase_at, finish, Decimal(200), "margin", 15, True, 14)["committed"] == "0.00"


def test_stock_demand_capacity_and_missing_quantity(sessions):
    with sessions.begin() as session:
        seed(session)
        row = {"product_id": SEED[0]["id"], "quantity": 10, "condition": "used", "identification": "confirmed"}
        value = component_estimates(session, "demo", [row, {**row, "quantity": None}], "ebay", 30, Decimal(0), Decimal(0))
        assert value["lines"][1]["unit"] is None
        if value["lines"][0]["unsold"] is not None:
            assert Decimal(value["lines"][0]["unsold"]) > 0
