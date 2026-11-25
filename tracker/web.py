import csv
import hashlib
import io
import json
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit
from uuid import uuid4

from fastapi import FastAPI, Form, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import select

from .availability import CLOSED, listing_availability
from .catalog import catalog, match
from .channels import CHANNELS, set_source, source_rules
from .connectors import connections, watch
from .db import Event, Job, make_sessions, now
from .documents import read_stock
from .ingest import EXPECTED, ROME, amount, clean_text, date, parse_file, preview, write_rows
from .monitoring import MARKETPLACES, save_watch, set_enabled, watches as product_watches
from .operations import (check_build, component_estimates, draft_from_inventory, duplicate_candidates,
                         duplicate_groups, market_changes, offer_cards, plan_budget, price_scenarios,
                         reference, request_matches, transition_inventory)
from .replay import default_window, replay
from .store import canonical, digest, enqueue, events, latest, record, utc
from .value import assess, dec, delivered, margin, rounded, sales_features, selling_costs

Mode = Literal["demo", "real"]
SOURCE = Literal["ebay", "subito", "vinted", "amazon", "aliexpress", "wallapop", "facebook", "local"]
ROOT = Path(__file__).resolve().parent
TEMPLATES = Jinja2Templates(directory=ROOT / "templates")
LABELS = {
    "available": "Disponibile", "unavailable": "Indisponibile", "unknown": "Da verificare",
    "reserved": "Riservato o in trattativa",
    "used": "Usato", "new": "Nuovo", "refurbished": "Ricondizionato", "broken": "Guasto",
    "part": "Ricambio", "lot": "Lotto", "single": "Articolo", "packaging": "Imballo",
    "accessory": "Accessorio", "purchased": "Acquistato", "verified": "Verificato", "listed": "Pubblicato",
    "repriced": "Prezzo rivisto", "sold": "Venduto", "returned": "Reso", "confirmed": "Confermato",
    "matched": "Abbinato", "uncertain": "Esito incerto", "done": "Completato", "pending": "In attesa",
    "retry": "Da riprovare", "running": "In corso", "failed": "Errore",
    "cancelled": "Annullato",
}


def euro(value):
    if value is None:
        return "—"
    return "€ " + f"{Decimal(value):,.2f}".replace(",", "_").replace(".", ",").replace("_", ".")


TEMPLATES.env.filters.update(
    euro=euro, label=lambda value: LABELS.get(value, value),
    rome=lambda value: utc(value if hasattr(value, "tzinfo") else date(value)).astimezone(ROME).strftime("%d/%m/%Y · %H:%M"),
    channel=lambda value: CHANNELS[value].name if value in CHANNELS else value,
)


def redirect(mode, path=""):
    return RedirectResponse(f"/{mode}{path}", status_code=303)


def row_for(session, mode, id, kind):
    row = session.get(Event, id)
    if row is None or row.mode != mode or row.kind != kind:
        raise HTTPException(404, "Record non trovato")
    return row


def stream_head(session, mode, kind, source, identifier):
    series = events(session, mode, kind, source=source, external_id=identifier)
    return series[-1] if series else None


def currency_input(value, *, default=None, required=False):
    result = amount(value)
    if result is None:
        if required:
            raise ValueError("Inserisci un importo valido")
        return default
    return dec(result)


def common(page, mode):
    return {"page": page, "mode": mode, "channels": CHANNELS}


def create_app(sessions=None):
    db = sessions or make_sessions()
    app = FastAPI(title="Mega Tracker di Offerte")
    app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")

    @app.middleware("http")
    async def local_forms(request, call_next):
        origin = request.headers.get("origin")
        if request.method not in {"GET", "HEAD", "OPTIONS"} and (
            request.headers.get("sec-fetch-site") == "cross-site" or
            (origin and urlsplit(origin).netloc != request.url.netloc)
        ):
            return PlainTextResponse("Origine della richiesta non valida", status_code=403)
        response = await call_next(request)
        response.headers["Referrer-Policy"] = "no-referrer"
        return response

    @app.exception_handler(ValueError)
    async def input_error(request, error):
        return PlainTextResponse(str(error), status_code=400)

    def render(request, page, mode, **context):
        return TEMPLATES.TemplateResponse(request=request, name=f"{page}.html",
                                          context={**common(page, mode), **context})

    @app.get("/", include_in_schema=False)
    def home():
        return redirect("demo")

    @app.get("/health")
    def health():
        with db() as session:
            session.execute(select(1))
        return {"ok": True}

    @app.get("/{mode}/api/offers")
    def offers_api(mode: Mode):
        with db() as session:
            return [{"id": card["offer"].id, "title": card["offer"].data["title"],
                     "source": card["offer"].source, **card["assessment"]} for card in offer_cards(session, mode)]

    @app.get("/{mode}")
    def dashboard(request: Request, mode: Mode, q: str = "", source: str = "", status: str = "current",
                  profit: str = "", percent: str = "",
                  maximum: str = "", evidence: int = 0, sort: str = "profit",
                  condition: str = "", confidence: str = "", observed_since: str = "", favorite: bool = False):
        since = date(observed_since)
        maximum = currency_input(maximum)
        from decimal import InvalidOperation
        try:
            profit = Decimal(profit) if profit else None
            percent = Decimal(percent) if percent else None
            if any(value is not None and not value.is_finite() for value in (profit, percent)):
                raise InvalidOperation
        except InvalidOperation:
            raise ValueError("Soglia di margine non valida") from None
        with db() as session:
            all_cards = offer_cards(session, mode)
            decisions = {row.external_id: row.data for row in latest(session, mode, "decision")}
            cards = []
            for card in all_cards:
                offer, best = card["offer"], card["best"]
                row = offer.data
                money = best["margin"] if best else None
                card["decision"] = decisions.get(reference(offer), {})
                if (q.casefold() not in (row["title"] + " " + row.get("description", "")).casefold() or
                    (source and source != offer.source) or (condition and condition != row["condition"]) or
                    (confidence and (not best or best["confidence"] != confidence)) or
                    (favorite and not card["decision"].get("favorite")) or
                    (since and utc(offer.known_at) < since)):
                    continue
                if status != "all" and (row["status"] == "unavailable" if status == "current" else row["status"] != status):
                    continue
                actual_pct = dec(money["percent"]) if money else None
                if (profit is not None and (not money or dec(money["profit"]) < profit) or
                    percent is not None and (actual_pct is None or actual_pct < percent) or
                    maximum is not None and (not card["assessment"]["purchase"] or dec(card["assessment"]["purchase"]) > maximum) or
                    evidence and (not best or best["comparables"]["count"] < evidence)):
                    continue
                cards.append(card)
            if sort == "fresh":
                cards.sort(key=lambda item: (utc(item["offer"].known_at), item["offer"].id), reverse=True)
            elif sort == "liquidity":
                from .value import liquidity
                for card in cards:
                    best = card["best"]
                    row = card["offer"].data
                    card["probability"] = liquidity(session, mode, card["product"]["id"], best["channel"],
                                                     dec(best["comparables"]["median"]), condition=row["condition"],
                                                     market=row["market"])["probability"] if best and card["product"] else None
                cards.sort(key=lambda item: dec(item["probability"]) if item["probability"] is not None else Decimal(-1), reverse=True)
            else:
                cards.sort(key=lambda item: dec(item["best"]["margin"]["profit"])
                           if item["best"] and item["best"]["margin"] else Decimal("-9999999999"), reverse=True)
            alerts = events(session, mode, "alert")
            return render(request, "dashboard", mode, cards=cards, q=q, active_source=source, sort=sort, status=status,
                          filters=request.query_params, alerts=len(alerts), monitored=len(all_cards),
                          review=sum(card["product"] is None for card in all_cards if card["offer"].data["status"] != "unavailable"))

    @app.get("/{mode}/offers/{id}")
    def details(request: Request, mode: Mode, id: int):
        with db() as session:
            picked = row_for(session, mode, id, "offer")
            history = events(session, mode, "offer", source=picked.source, external_id=picked.external_id)
            offer = history[-1]
            products = catalog(session, mode)
            evaluation = assess(session, mode, offer)
            decision = stream_head(session, mode, "decision", "local", reference(offer))
            images = events(session, mode, "ocr", source=offer.source, external_id=offer.external_id)
            candidates = duplicate_candidates(offer, latest(session, mode, "offer"))
            confirmed_group = duplicate_groups(session, mode).get(reference(offer))
            group = [row for row in latest(session, mode, "offer")
                     if row.id != offer.id and confirmed_group and duplicate_groups(session, mode).get(reference(row)) == confirmed_group]
            scenarios = {}
            for estimate in evaluation["estimates"]:
                scenarios[estimate["channel"]] = price_scenarios(
                    session, mode, evaluation["matching"]["product_id"], estimate["channel"],
                    dec(estimate["comparables"]["median"]), delivered(offer.data),
                    offer.data["condition"], offer.data["market"],
                )
            jobs = session.scalars(select(Job).where(Job.mode == mode, Job.kind == "refresh",
                                                     Job.status.in_(("pending", "running", "retry"))).order_by(Job.run_at))
            next_check = next((job.run_at for job in jobs if job.data == {"source": offer.source, "id": offer.external_id}), None)
            return render(request, "details", mode, offer=offer, history=history, evaluation=evaluation, products=products,
                          decision=decision, images=images, candidates=candidates, group=group,
                          scenarios=scenarios, next_check=next_check)

    @app.post("/{mode}/offers/{id}/match")
    def resolve(mode: Mode, id: int, product_id: str = Form("")):
        with db.begin() as session:
            offer = row_for(session, mode, id, "offer")
            if product_id and product_id not in catalog(session, mode):
                raise ValueError("Variante sconosciuta")
            token = record(session, mode, "matching", offer.source, offer.external_id,
                           {"product_id": product_id or None}, identity=uuid4().hex)
            enqueue(session, mode, "evaluate", f"evaluate:match:{token}", {"source": offer.source, "id": offer.external_id})
        return redirect(mode, f"/offers/{id}")

    @app.post("/{mode}/offers/{id}/decision")
    def offer_decision(mode: Mode, id: int, state: Literal["watch", "consider", "skip"] = Form(),
                       favorite: bool = Form(False), note: str = Form("", max_length=500)):
        with db.begin() as session:
            offer = row_for(session, mode, id, "offer")
            record(session, mode, "decision", "local", reference(offer),
                   {"state": state, "favorite": favorite, "note": clean_text(note)}, identity=uuid4().hex)
        return redirect(mode, f"/offers/{id}")

    @app.post("/{mode}/offers/{id}/duplicate")
    def mark_duplicate(mode: Mode, id: int, other: int = Form()):
        with db.begin() as session:
            first, second = (row_for(session, mode, value, "offer") for value in (id, other))
            if reference(first) == reference(second):
                raise ValueError("Scegli un altro annuncio")
            groups = duplicate_groups(session, mode)
            target = groups.get(reference(first)) or groups.get(reference(second)) or uuid4().hex
            old = {groups.get(reference(first)), groups.get(reference(second))} - {None, target}
            members = {reference(first), reference(second)}
            members.update(key for key, value in groups.items() if value in old)
            for member in sorted(members):
                record(session, mode, "duplicate", "local", member, {"group": target}, identity=uuid4().hex)
        return redirect(mode, f"/offers/{id}")

    @app.post("/{mode}/offers/{id}/ocr")
    def offer_ocr(mode: Mode, id: int, file: UploadFile):
        raw = file.file.read(8_000_001)
        if not raw or len(raw) > 8_000_000:
            raise ValueError("Carica un’immagine fino a 8 MB")
        from PIL import Image, UnidentifiedImageError
        from .documents import image_evidence

        try:
            with Image.open(io.BytesIO(raw)) as image:
                if image.width * image.height > 30_000_000:
                    raise ValueError("Immagine troppo grande")
                evidence = image_evidence(image)
        except UnidentifiedImageError as error:
            raise ValueError("Formato immagine sconosciuto") from error
        evidence["text"] = clean_text(evidence["text"])
        for box in evidence["boxes"]:
            box["word"] = clean_text(box["word"])
        with db.begin() as session:
            picked = row_for(session, mode, id, "offer")
            offer = stream_head(session, mode, "offer", picked.source, picked.external_id)
            identity = hashlib.sha256(raw).hexdigest()
            if record(session, mode, "ocr", offer.source, offer.external_id, evidence, identity=identity):
                observed = record(session, mode, "offer", offer.source, offer.external_id,
                                  {**offer.data, "ocr": evidence["text"], "photo_hash": evidence["photo_hash"]},
                                  identity=f"ocr:{identity}:{offer.id}", effective=utc(offer.effective_at))
                enqueue(session, mode, "evaluate", f"evaluate:ocr:{observed}", {"source": offer.source, "id": offer.external_id})
        return redirect(mode, f"/offers/{id}")

    @app.get("/{mode}/review")
    def review(request: Request, mode: Mode):
        with db() as session:
            cards = [card for card in offer_cards(session, mode) if card["offer"].data["status"] != "unavailable"
                     and (not card["product"] or duplicate_candidates(card["offer"], latest(session, mode, "offer")))]
            return render(request, "review", mode, cards=cards, errors=events(session, mode, "import_error")[-20:])

    @app.get("/{mode}/sources")
    def sources(request: Request, mode: Mode, q: str = "RTX 4070"):
        with db() as session:
            sales = latest(session, mode, "sale")
            from .codex_index import enabled as codex_enabled

            return render(request, "sources", mode, query=q, connections={key: connections(key) for key in CHANNELS},
                          codex_enabled=codex_enabled(),
                          policies=source_rules(session, mode), watches=latest(session, mode, "watch"),
                          products=catalog(session, mode), product_watches=product_watches(session, mode),
                          marketplaces=MARKETPLACES,
                          collections=events(session, mode, "collection")[-12:],
                          errors=events(session, mode, "collection_error")[-8:],
                          batches=events(session, mode, "import")[-10:],
                          jobs=list(session.scalars(select(Job).where(Job.mode == mode).order_by(Job.id.desc()).limit(15))),
                          market=market_changes(session, mode),
                          sale_evidence=[item for item in sales if item.data["verified"] and item.data["paid_price"] is None])

    @app.post("/{mode}/sources/watch")
    def add_watch(mode: Mode, source: SOURCE = Form(), query: str = Form(min_length=2, max_length=120),
                  minutes: int = Form(60, ge=15, le=10080)):
        with db.begin() as session:
            watch(session, mode, query.strip(), source, minutes)
        return redirect(mode, "/sources")

    @app.post("/{mode}/sources/products")
    def add_product_watch(mode: Mode, product_id: str = Form(), sources: list[str] = Form(),
                          minutes: int = Form(60, ge=15, le=10080), query: str = Form("", max_length=120),
                          max_cost: str = Form(""), min_profit: str = Form(""), signals: bool = Form(False)):
        with db.begin() as session:
            save_watch(session, mode, product_id, sources, minutes, query=query, max_cost=max_cost,
                       min_profit=min_profit, signals=signals)
        return redirect(mode, "/sources")

    @app.post("/{mode}/sources/products/{product_id}/state")
    def product_watch_state(mode: Mode, product_id: str, enabled: bool = Form()):
        with db.begin() as session:
            set_enabled(session, mode, product_id, enabled)
        return redirect(mode, "/sources")

    @app.post("/{mode}/sources/policy")
    def policy(mode: Mode, source: SOURCE = Form(), history: bool = Form(False), training: bool = Form(False)):
        with db.begin() as session:
            set_source(session, mode, source, history=history, training=training)
        return redirect(mode, "/sources")

    @app.post("/{mode}/imports")
    def imports(request: Request, mode: Mode, file: UploadFile, source: SOURCE = Form(),
                kind: Literal["offer", "sale", "stock"] = Form(), columns: str = Form("", max_length=5000),
                historical: bool = Form(False)):
        raw = file.file.read(12_000_001)
        if not raw or len(raw) > 12_000_000:
            raise ValueError("Carica un file fino a 12 MB")
        mapping = json.loads(columns) if columns.strip() else {}
        if not isinstance(mapping, dict) or not set(mapping) <= EXPECTED or not all(isinstance(v, str) for v in mapping.values()):
            raise ValueError("La mappatura deve indicare campo del tracker e nome della colonna")
        filename = file.filename or ""
        rows = read_stock(raw, filename) if kind == "stock" else parse_file(raw, filename)
        if len(rows) > 10000:
            raise ValueError("Dividi l’import in file da al massimo 10.000 righe")
        if not historical:
            columns_to_remove = {mapping.get("observed_at", "observed_at").strip().casefold()}
            rows = [(number, {key: value for key, value in row.items() if str(key).strip().casefold() not in columns_to_remove})
                    for number, row in rows]
        valid, errors = preview(rows, mapping, kind)
        batch = digest(f"{kind}:{canonical(mapping)}:{historical}:" + hashlib.sha256(raw).hexdigest())
        with db.begin() as session:
            if source != "local" and not source_rules(session, mode).get(source, {}).get("history"):
                raise ValueError("Abilita la conservazione per questa fonte prima di importare")
            token = record(session, mode, "import_preview", source, batch,
                           {"kind": kind, "errors": errors, "rows": [
                               [number, data, effective.isoformat(), known.isoformat(), old.isoformat() if old else None]
                               for number, (data, effective, known, old) in valid
                           ]}, identity=batch)
            if token is None:
                token = stream_head(session, mode, "import_preview", source, batch).id
            return render(request, "import", mode, token=token, kind=kind, source=source,
                          valid=valid[:25], count=len(valid), errors=errors)

    @app.post("/{mode}/imports/{id}")
    def confirm_import(mode: Mode, id: int):
        with db.begin() as session:
            item = row_for(session, mode, id, "import_preview")
            if not source_rules(session, mode).get(item.source, {}).get("history") and item.source != "local":
                raise ValueError("Abilita la conservazione per questa fonte prima di confermare")
            kind, errors = item.data["kind"], item.data["errors"]
            valid = [(number, (data, date(effective), date(known), date(old)))
                     for number, data, effective, known, old in item.data["rows"]]
            write_rows(session, mode, item.source, kind, valid, errors, item.external_id)
        return redirect(mode, "/lots" if kind == "stock" else "/sources")

    @app.get("/{mode}/imports/{id}/errors")
    def export_errors(mode: Mode, id: int):
        with db() as session:
            errors = row_for(session, mode, id, "import_preview").data["errors"]
            output = io.StringIO()
            writer = csv.writer(output, delimiter=";")
            writer.writerow(("Riga", "Motivo"))
            writer.writerows((item["row"], item["message"]) for item in errors)
            return Response(output.getvalue(), media_type="text/csv",
                            headers={"Content-Disposition": "attachment; filename=scarti.csv"})

    @app.get("/{mode}/alerts")
    def alerts(request: Request, mode: Mode):
        with db() as session:
            availability = listing_availability(session, mode)
            visible, closed = [], []
            for item in reversed(events(session, mode, "monitor_signal")):
                check = availability.get((item.source, item.external_id))
                group = closed if check and check.status in CLOSED else visible
                group.append((item, check))
            return render(request, "alerts", mode, alerts=events(session, mode, "alert")[::-1],
                          monitor_signals=visible, closed_signals=closed,
                          request_alerts=events(session, mode, "request_alert")[::-1])

    from .web_workflows import register
    register(app, db, render)
    return app


app = create_app()
