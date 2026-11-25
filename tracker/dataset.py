import json
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from sqlalchemy import select

from .catalog import catalog, match
from .channels import source_rules
from .db import Event, now
from .store import canonical, digest, events, latest, utc
from .value import eur, rounded, sales_features


def examples(session, mode, cutoff=None):
    cutoff = utc(cutoff or now())
    active = source_rules(session, mode)
    allowed = {source for source, rule in source_rules(session, mode, cutoff).items()
               if rule["training"] and active.get(source, {}).get("training")}
    rows, excluded = [], {}

    def skip(reason):
        excluded[reason] = excluded.get(reason, 0) + 1

    for sale in latest(session, mode, "sale", cutoff=cutoff):
        info = sale.data
        paid = eur(info, "paid_price")
        if sale.source not in allowed:
            skip("fonte_non_ammessa")
            continue
        if not info["verified"] or info["status"] != "sold" or paid is None:
            skip("prezzo_pagato_non_verificato")
            continue
        decision = info.get("decision_at") or info.get("published_at")
        decision = utc(datetime.fromisoformat(decision)) if decision else None
        if decision is None or not decision < utc(sale.effective_at):
            skip("decisione_non_ricostruibile")
            continue
        products = catalog(session, mode, decision)
        snapshots = events(session, mode, "offer", source=sale.source, external_id=info["listing_id"],
                           cutoff=decision) if info.get("listing_id") else []
        if snapshots:
            snapshot = snapshots[-1].data
        elif info.get("decision_title") and info.get("decision_condition", "unknown") != "unknown":
            snapshot = {"title": info["decision_title"], "condition": info["decision_condition"],
                        "sale_format": info.get("decision_format", ""), "market": info["market"]}
        else:
            skip("snapshot_decisione_assente")
            continue
        product_id = match(snapshot, products)["product_id"]
        if product_id is None:
            skip("variante_non_ricostruibile")
            continue
        order = info["order_id"]
        group = digest(f"{sale.source}:{order or sale.external_id}")
        channel = (info.get("destination") or sale.source).casefold()
        features = sales_features(
            session, mode, product_id, snapshot["condition"], snapshot["market"], channel, decision,
            exclude=(sale.source, sale.external_id), exclude_order=(sale.source, order) if order else None,
            allowed_sources=allowed, sale_format=snapshot.get("sale_format", ""),
        )
        first_observed = min(utc(item.known_at) for item in events(
            session, mode, "sale", cutoff=cutoff, source=sale.source, external_id=sale.external_id
        ))
        rows.append({
            "event_id": sale.id, "source": sale.source, "order_group": group, "product_id": product_id,
            "category": products[product_id]["category"], "condition": snapshot["condition"],
            "market": snapshot["market"], "channel": channel, "sale_format": snapshot.get("sale_format", ""),
            "decision_at": decision, "sold_at": utc(sale.effective_at),
            "available_at": first_observed, "target_snapshot_at": utc(sale.known_at),
            "unit_paid_eur": Decimal(rounded(paid / info["quantity"])), "comparison_count": features["count"],
            "baseline_eur": Decimal(features["median"]) if features["median"] is not None else None,
            "comparison_event_ids": [item["id"] for item in features["evidence"]],
        })
    rows.sort(key=lambda row: (row["decision_at"], row["order_group"], row["event_id"]))
    return rows, {"mode": mode, "cutoff": cutoff.isoformat(), "sources": sorted(allowed), "rows": len(rows),
                  "excluded": excluded, "schema_version": 1, "catalog": digest(canonical(catalog(session, mode, cutoff))),
                  "generated_at": now().isoformat(), "target": "unit_paid_eur"}


def export_parquet(session, mode, filename, cutoff=None):
    import pyarrow as pa
    import pyarrow.parquet as pq

    rows, manifest = examples(session, mode, cutoff)
    stamp = pa.timestamp("us", tz="UTC")
    schema = pa.schema([
        ("event_id", pa.int64()), ("source", pa.string()), ("order_group", pa.string()),
        ("product_id", pa.string()), ("category", pa.string()), ("condition", pa.string()),
        ("market", pa.string()), ("channel", pa.string()), ("sale_format", pa.string()),
        ("decision_at", stamp), ("sold_at", stamp), ("available_at", stamp), ("target_snapshot_at", stamp),
        ("unit_paid_eur", pa.decimal128(20, 2)), ("comparison_count", pa.int32()),
        ("baseline_eur", pa.decimal128(20, 2)), ("comparison_event_ids", pa.list_(pa.int64())),
    ])
    manifest["schema"] = str(schema)
    target = Path(filename)
    target.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(pa.Table.from_pylist(rows, schema=schema), target)
    target.with_suffix(target.suffix + ".json").write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")
    return manifest


def archive_events(session, mode, filename):
    rules = source_rules(session, mode)
    query = select(Event).where(Event.mode == mode).order_by(Event.id)
    with Path(filename).open("w", encoding="utf-8") as output:
        for item in session.scalars(query.execution_options(yield_per=200)):
            if item.source != "local" and not rules.get(item.source, {}).get("history"):
                continue
            output.write(canonical({
                "id": item.id, "kind": item.kind, "source": item.source, "external_id": item.external_id,
                "effective_at": utc(item.effective_at).isoformat(), "known_at": utc(item.known_at).isoformat(),
                "ingested_at": utc(item.ingested_at).isoformat(), "data": item.data,
            }) + "\n")
