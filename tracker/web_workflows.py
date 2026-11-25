from datetime import timedelta
from decimal import Decimal
from uuid import uuid4

from fastapi import Form, HTTPException, Request
from fastapi.responses import PlainTextResponse
from sqlalchemy import delete

from .catalog import catalog, match
from .db import Reservation, now
from .ingest import ROME, clean_text, date, preview, write_rows
from .operations import (check_build, component_estimates, draft_from_inventory, duplicate_groups,
                         offer_cards, plan_budget, reference, request_matches, transition_inventory)
from .replay import default_window, replay
from .store import events, insert_once, latest, record, utc
from .value import dec, delivered, margin, rounded, sales_features, selling_costs
from .web import Mode, SOURCE, currency_input, redirect, row_for, stream_head


def register(app, db, render):
    @app.get("/{mode}/lots")
    def lots(request: Request, mode: Mode, horizon: int = 30, target: str = "40",
             costs: str = "20", channel: SOURCE = "ebay", stock_batch: str = ""):
        if not 1 <= horizon <= 365:
            raise ValueError("Orizzonte tra 1 e 365 giorni")
        target_money = currency_input(target, required=True)
        expenses = currency_input(costs, required=True)
        with db() as session:
            products = catalog(session, mode)
            forecasts = []
            for row in latest(session, mode, "lot"):
                original = stream_head(session, mode, "offer", row.source, row.external_id)
                purchase = delivered(original.data) if original else None
                parts = component_estimates(session, mode, row.data["components"], channel, horizon, expenses, target_money)
                whole_id = row.data.get("whole_product_id")
                base = sales_features(session, mode, whole_id, original.data["condition"] if original else "used",
                                      "IT", channel, now()) if whole_id else {}
                whole_net = margin(dec(base.get("low")), purchase, *selling_costs(mode, channel))
                forecasts.append({"event": row, "parts": parts, "whole": whole_net, "original": original,
                                  "purchase": rounded(purchase), "parts_profit_all": rounded(dec(parts["all_net"]) - purchase)
                                  if purchase is not None else None})
            stock = latest(session, mode, "stock")
            batches = sorted({row.data.get("batch", "") for row in stock})
            batch = stock_batch or next(iter(batches), "")
            reviews = {row.external_id: row.data for row in latest(session, mode, "stock_match")}
            stock_items = []
            for row in stock:
                if row.data.get("batch") != batch:
                    continue
                reviewed = reviews.get(reference(row))
                product = reviewed["product_id"] if reviewed else match(row.data, products)["product_id"]
                stock_items.append({**row.data, **(reviewed or {}), "id": row.id, "product_id": product,
                                    "identification": "confirmed" if reviewed and product else "matched" if product else "unknown"})
            return render(request, "lots", mode, forecasts=forecasts, stock=component_estimates(
                session, mode, stock_items, channel, horizon, expenses, target_money) if stock_items else None,
                batches=batches, active_batch=batch, products=products, channel=channel,
                horizon=horizon, target=target, costs=costs,
                offers=[card for card in offer_cards(session, mode) if card["product"] and card["offer"].data["status"] == "available"],
                lots_candidates=[row for row in latest(session, mode, "offer") if match(row.data, products)["type"] == "lot"],
                inventory=[row for row in latest(session, mode, "inventory") if row.data.get("product_id")
                           and row.data["state"] in {"purchased", "verified"}],
                builds=latest(session, mode, "build"))

    @app.post("/{mode}/lots/component")
    def lot_component(mode: Mode, offer_id: int = Form(), product_id: str = Form(""),
                      position: int = Form(-1, ge=-1, le=100), quantity: int = Form(1, ge=1, le=1000),
                      evidence: str = Form("", max_length=500), whole_product_id: str = Form(""),
                      remove: bool = Form(False)):
        with db.begin() as session:
            offer = row_for(session, mode, offer_id, "offer")
            products = catalog(session, mode)
            if (product_id and product_id not in products) or (whole_product_id and whole_product_id not in products):
                raise ValueError("Variante sconosciuta")
            head = stream_head(session, mode, "lot", offer.source, offer.external_id)
            components = list(head.data["components"]) if head else []
            item = {"product_id": product_id or None, "quantity": quantity, "evidence": clean_text(evidence),
                    "condition": "used", "identification": "confirmed" if product_id else "unknown"}
            if position == -1 and not remove:
                components.append(item)
            elif 0 <= position < len(components):
                if remove:
                    components.pop(position)
                else:
                    components[position] = item
            else:
                raise ValueError("Posizione del componente sconosciuta")
            record(session, mode, "lot", offer.source, offer.external_id,
                   {"title": offer.data["title"], "components": components,
                    "whole_product_id": whole_product_id or (head.data.get("whole_product_id") if head else None)},
                   identity=uuid4().hex)
        return redirect(mode, "/lots")

    @app.post("/{mode}/stock/{id}/review")
    def stock_review(mode: Mode, id: int, product_id: str = Form(""), quantity: int | None = Form(None, ge=1, le=100000),
                     condition: str = Form("unknown")):
        if condition not in {"used", "new", "refurbished", "broken", "unknown"}:
            raise ValueError("Condizione sconosciuta")
        with db.begin() as session:
            row = row_for(session, mode, id, "stock")
            if product_id and product_id not in catalog(session, mode):
                raise ValueError("Variante sconosciuta")
            record(session, mode, "stock_match", "local", reference(row),
                   {"product_id": product_id or None, "quantity": quantity, "condition": condition}, identity=uuid4().hex)
        return redirect(mode, f"/lots?stock_batch={row.data['batch']}")

    @app.post("/{mode}/builds")
    def create_build(mode: Mode, choices: list[str] = Form(), labor: str = Form("0"),
                     title: str = Form("Piano PC", max_length=150), reserve: bool = Form(False)):
        assembly = currency_input(labor, required=True)
        with db.begin() as session:
            cards = {card["offer"].id: card for card in offer_cards(session, mode)}
            catalog_items = catalog(session, mode)
            groups = duplicate_groups(session, mode)
            refs, keys, products, missing = [], [], [], []
            cost = assembly
            if not 1 <= len(set(choices)) <= 12:
                raise ValueError("Seleziona da uno a dodici componenti")
            for choice in set(choices):
                kind, number = choice.split(":")
                if kind == "offer":
                    card = cards.get(int(number))
                    if not card or not card["product"] or card["offer"].data["status"] != "available":
                        raise ValueError("Offerta non disponibile")
                    item = card["offer"]
                    ref = reference(item)
                    product = card["product"]
                    value = delivered(item.data)
                    key = groups.get(ref, ref)
                elif kind == "inventory":
                    item = row_for(session, mode, int(number), "inventory")
                    item = stream_head(session, mode, "inventory", "local", item.external_id)
                    if item.data["state"] not in {"purchased", "verified"}:
                        raise ValueError("Articolo di inventario non disponibile")
                    ref = f"inventory:{item.external_id}"
                    product = catalog_items.get(item.data.get("product_id"))
                    value = dec(item.data.get("purchase"))
                    key = item.data.get("group") or ref
                else:
                    raise ValueError("Tipo di componente sconosciuto")
                if key in keys:
                    raise ValueError("La stessa unità compare due volte")
                if not product:
                    raise ValueError("Variante non confermata")
                refs.append(ref)
                keys.append(key)
                products.append(product)
                if value is None:
                    missing.append(item.data["title"])
                else:
                    cost += value
            checks = check_build(products)
            identifier = uuid4().hex
            if reserve:
                if any(item["status"] == "incompatibile" for item in checks):
                    raise ValueError("Risolvi le incompatibilità prima di prenotare il piano")
                for key in sorted(keys):
                    if not insert_once(session, Reservation, ["mode", "unit"], {"mode": mode, "unit": key, "plan": identifier}):
                        raise ValueError("Un componente è già assegnato a un altro piano")
            whole = catalog_items.get("pc-am4-4070")
            comparison = sales_features(session, mode, whole["id"], "used", "IT", "ebay", now()) if whole and {
                "rtx4070-asus-dual", "ryzen5600", "kit-am4", "case-650"
            } <= {item["id"] for item in products} else {}
            estimate = margin(dec(comparison.get("median")), cost if not missing else None, *selling_costs(mode, "ebay"))
            record(session, mode, "build", "local", identifier,
                   {"title": clean_text(title), "refs": refs, "names": [item["name"] for item in products],
                    "known_cost": rounded(cost), "missing": missing, "checks": checks,
                    "reserved": reserve, "assembly": rounded(assembly), "margin": estimate})
        return redirect(mode, "/lots")

    @app.post("/{mode}/builds/{id}/release")
    def release_build(mode: Mode, id: int):
        with db.begin() as session:
            item = row_for(session, mode, id, "build")
            item = stream_head(session, mode, "build", "local", item.external_id)
            session.execute(delete(Reservation).where(Reservation.mode == mode, Reservation.plan == item.external_id))
            record(session, mode, "build", "local", item.external_id, {**item.data, "reserved": False}, identity=uuid4().hex)
        return redirect(mode, "/lots")

    @app.get("/{mode}/requests")
    def requests(request: Request, mode: Mode):
        with db() as session:
            return render(request, "requests", mode, matches=request_matches(session, mode, offer_cards(session, mode)),
                          products=catalog(session, mode), today=now().astimezone(ROME).date().isoformat())

    @app.post("/{mode}/requests")
    def add_request(mode: Mode, product_id: str = Form(), budget: str = Form(),
                    quantity: int = Form(1, ge=1, le=1000), conditions: list[str] = Form(),
                    area: str = Form("", max_length=100), expires: str = Form(), opt_in: bool = Form(False)):
        money = currency_input(budget, required=True)
        expiry = date(expires)
        if expiry is None or expiry.astimezone(ROME).date() < now().astimezone(ROME).date():
            raise ValueError("Inserisci una scadenza valida")
        if set(conditions) - {"used", "new", "refurbished"}:
            raise ValueError("Condizione sconosciuta")
        with db.begin() as session:
            if product_id not in catalog(session, mode):
                raise ValueError("Scegli una variante")
            record(session, mode, "request", "local", uuid4().hex,
                   {"product_id": product_id, "budget": rounded(money), "quantity": quantity,
                    "conditions": conditions, "area": clean_text(area),
                    "expires": expiry.astimezone(ROME).date().isoformat(), "opt_in": opt_in})
            if opt_in:
                from .worker import snapshot_request_alerts
                for card in offer_cards(session, mode):
                    snapshot_request_alerts(session, mode, card["offer"], card["assessment"])
        return redirect(mode, "/requests")

    @app.get("/{mode}/budget")
    def budget(request: Request, mode: Mode, capital: str = "420", minimum: str = "20",
               concentration: int = 100, horizon: int = 30, risk: str = "balanced"):
        if not 1 <= concentration <= 100 or not 1 <= horizon <= 365:
            raise ValueError("Controlla limiti e orizzonte")
        money = currency_input(capital, required=True)
        profit = currency_input(minimum, required=True)
        with db() as session:
            cards = offer_cards(session, mode)
            base = (session, mode, cards, money, profit, Decimal(concentration) / 100, horizon)
            standard = plan_budget(*base, prudent=risk == "prudent")
            prudent = plan_budget(*base, prudent=True)
            return render(request, "budget", mode, standard=standard, prudent=prudent,
                          capital=capital, minimum=minimum, concentration=concentration, horizon=horizon, risk=risk)

    @app.get("/{mode}/inventory")
    def inventory(request: Request, mode: Mode):
        with db() as session:
            items = latest(session, mode, "inventory")[::-1]
            suggestions = {}
            products = catalog(session, mode)
            for item in items:
                data = item.data
                if data.get("product_id") not in products or data["state"] not in {"listed", "repriced", "verified"}:
                    continue
                channel = data.get("channel") or "ebay"
                comps = sales_features(session, mode, data["product_id"], data.get("condition", "used"), "IT", channel, now())
                if comps["count"]:
                    suggestions[item.id] = comps
            return render(request, "inventory", mode, items=items, products=products, suggestions=suggestions,
                          offers=[card for card in offer_cards(session, mode) if card["offer"].data["status"] == "available"],
                          drafts=latest(session, mode, "draft")[::-1])

    @app.post("/{mode}/inventory")
    def register_purchase(mode: Mode, title: str = Form("", max_length=250), cost: str = Form(),
                          specs: str = Form("", max_length=2500), product_id: str = Form(""),
                          offer_id: int | None = Form(None)):
        money = currency_input(cost, required=True)
        with db.begin() as session:
            offer = row_for(session, mode, offer_id, "offer") if offer_id else None
            if offer:
                offer = stream_head(session, mode, "offer", offer.source, offer.external_id)
                group = duplicate_groups(session, mode).get(reference(offer), reference(offer))
                existing = [row for row in latest(session, mode, "inventory") if row.data.get("group") == group
                            and row.data["state"] in {"purchased", "verified", "listed", "repriced", "returned"}]
                if existing:
                    raise ValueError("Unità già registrata in inventario")
            else:
                group = uuid4().hex
            if not title.strip() and offer is None:
                raise ValueError("Inserisci un titolo o seleziona un’offerta")
            products = catalog(session, mode)
            if product_id and product_id not in products:
                raise ValueError("Variante sconosciuta")
            transition_inventory(session, mode, uuid4().hex, "purchased", {
                "title": clean_text(title.strip()) or offer.data["title"], "purchase": rounded(money), "group": group,
                "specs": clean_text(specs), "product_id": product_id or (match(offer.data, products)["product_id"] if offer else None),
                "origin": reference(offer) if offer else "",
                "condition": offer.data["condition"] if offer else "used",
            })
        return redirect(mode, "/inventory")

    @app.post("/{mode}/inventory/{id}")
    def inventory_state(mode: Mode, id: int, state: str = Form(), channel: SOURCE = Form("ebay"),
                        price: str = Form(""), fees: str = Form(""), outbound: str = Form(""), extras: str = Form(""),
                        note: str = Form("", max_length=500)):
        with db.begin() as session:
            row = row_for(session, mode, id, "inventory")
            changes = {"note": clean_text(note)}
            if state in {"listed", "repriced", "sold"}:
                changes["channel"] = channel
                changes["sale_price" if state == "sold" else "ask"] = rounded(currency_input(price, required=True))
            if state == "sold":
                changes.update({key: rounded(currency_input(value, required=True))
                                for key, value in (("fees", fees), ("outbound", outbound), ("extras", extras))})
            history = events(session, mode, "inventory", source="local", external_id=row.external_id)
            transition_inventory(session, mode, row.external_id, state, changes)
            if state == "sold" or (state == "returned" and history[-1].data["state"] == "sold"):
                current = history[-1].data
                destination = changes.get("channel") or current["channel"]
                existing_sale = stream_head(session, mode, "sale", "local", f"inventory:{row.external_id}")
                clock = now()
                listed = next((item for item in reversed(history) if item.data["state"] == "listed"
                               and item.data["channel"] == destination), None)
                product = catalog(session, mode).get(current.get("product_id"))
                title = product["name"] if product else current["title"]
                sale = {"external_id": f"inventory:{row.external_id}", "title": title,
                        "condition": current.get("condition", "used"), "market": "IT", "quantity": 1,
                        "verified": True, "status": "sold" if state == "sold" else "returned",
                        "paid_price": changes.get("sale_price") or current["sale_price"], "sale_format": "fixed",
                        "destination": destination, "sold_at": (
                            utc(existing_sale.effective_at).isoformat() if existing_sale and state == "returned" else clock.isoformat()
                        )}
                if listed:
                    decision_time = utc(listed.effective_at)
                    original_product = catalog(session, mode, decision_time).get(listed.data.get("product_id"))
                    sale.update(decision_at=decision_time.isoformat(),
                                decision_title=original_product["name"] if original_product else listed.data["title"],
                                decision_condition=sale["condition"], decision_format="fixed")
                valid, errors = preview([(1, sale)], {}, "sale")
                if errors:
                    raise ValueError("Esito di vendita non registrabile")
                write_rows(session, mode, "local", "sale", valid, errors, uuid4().hex)
        return redirect(mode, "/inventory")

    @app.post("/{mode}/inventory/{id}/draft")
    def create_draft(mode: Mode, id: int, channel: SOURCE = Form(), price: str = Form()):
        value = rounded(currency_input(price, required=True))
        with db.begin() as session:
            original = row_for(session, mode, id, "inventory")
            item = stream_head(session, mode, "inventory", "local", original.external_id)
            if item.data["state"] in {"sold", "returned"}:
                raise ValueError("L’articolo non risulta disponibile")
            record(session, mode, "draft", "local", uuid4().hex, draft_from_inventory(item, channel, value))
        return redirect(mode, "/inventory")

    @app.post("/{mode}/drafts/{id}")
    def edit_draft(mode: Mode, id: int, title: str = Form(min_length=1, max_length=250),
                   description: str = Form("", max_length=2500), price: str = Form(), channel: SOURCE = Form()):
        from .channels import CHANNELS
        value = rounded(currency_input(price, required=True))
        with db.begin() as session:
            row = row_for(session, mode, id, "draft")
            record(session, mode, "draft", "local", row.external_id,
                   {**row.data, "title": clean_text(title), "description": clean_text(description),
                    "price": value, "channel": channel, "publisher": CHANNELS[channel].seller}, identity=uuid4().hex)
        return redirect(mode, "/inventory")

    @app.get("/{mode}/drafts/{id}.txt")
    def export_draft(mode: Mode, id: int):
        with db() as session:
            row = row_for(session, mode, id, "draft")
            data = stream_head(session, mode, "draft", "local", row.external_id).data
        return PlainTextResponse(f"{data['title']}\n\n{data['description']}\n\nPrezzo: {data['price']} EUR\n",
                                 headers={"Content-Disposition": "attachment; filename=annuncio.txt"})

    @app.get("/{mode}/simulation")
    def simulation(request: Request, mode: Mode, start: str = "", end: str = "", capital: str = "420",
                   latency: int = 15, sale_days: int = 14, availability: bool = True, live: int | None = None):
        if not 0 <= latency <= 1440 or not 1 <= sale_days <= 365:
            raise ValueError("Controlla latenza e giorni d’incasso")
        left, right = default_window()
        left, right = date(start) or left, date(end) or right
        money = currency_input(capital, required=True)
        with db() as session:
            current_run = row_for(session, mode, live, "simulation") if live else None
            if current_run and current_run.data["type"] == "live":
                left, right = date(current_run.data["start"]), now()
                money, latency, sale_days, availability = (
                    dec(current_run.data["capital"]), current_run.data["latency"], current_run.data["sale_days"],
                    current_run.data["availability"],
                )
            if left > right or right > now() or right - left > timedelta(days=365):
                raise ValueError("Seleziona un periodo passato fino a 365 giorni")
            results = [replay(session, mode, left, right, money, strategy, latency, availability, sale_days)
                       for strategy in ("margin", "liquidity")]
            return render(request, "simulation", mode, results=results, start=left, end=right, capital=rounded(money),
                          latency=latency, sale_days=sale_days, availability=availability, live=live,
                          runs=latest(session, mode, "simulation")[::-1][:10])

    @app.post("/{mode}/simulation")
    def save_run(mode: Mode, start: str = Form(), end: str = Form(), capital: str = Form(),
                 latency: int = Form(15, ge=0, le=1440), sale_days: int = Form(14, ge=1, le=365),
                 availability: bool = Form(False), live: bool = Form(False)):
        money = currency_input(capital, required=True)
        left, right = (now(), now()) if live else (date(start), date(end))
        if left is None or right is None or left > right or right > now() or right - left > timedelta(days=365):
            raise ValueError("Periodo di simulazione non valido")
        with db.begin() as session:
            results = [] if live else [replay(session, mode, left, right, money, strategy, latency, availability, sale_days)
                                       for strategy in ("margin", "liquidity")]
            token = record(session, mode, "simulation", "local", uuid4().hex,
                           {"type": "live" if live else "replay", "start": left.isoformat(), "end": right.isoformat(),
                            "capital": rounded(money), "latency": latency, "sale_days": sale_days,
                            "availability": availability, "results": results})
        return redirect(mode, f"/simulation?live={token}" if live else "/simulation")
