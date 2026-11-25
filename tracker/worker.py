import os
import time
from datetime import timedelta
from decimal import Decimal
from uuid import uuid4

import httpx
from sqlalchemy import and_, or_, select, update

from .connectors import FetchError, collect, direct_connection, schedule
from .monitoring import save_signal, selection_for
from .db import Event, Job, now
from .store import enqueue, events, latest, record, utc
from .value import assess, dec, save_assessment

LEASE_SECONDS = 90
ATTEMPTS = 5


def claim(session, mode):
    timestamp = now()
    due = or_(and_(Job.status.in_(("pending", "retry")), Job.run_at <= timestamp),
              and_(Job.status == "running", Job.lease_until <= timestamp))
    token = uuid4().hex
    first = select(Job.id).where(Job.mode == mode, due).order_by(Job.run_at, Job.id).limit(1)
    if session.bind.dialect.name == "postgresql":
        first = first.with_for_update(skip_locked=True)
    return session.scalars(update(Job).where(Job.id == first.scalar_subquery(), due)
                           .values(status="running", lease_token=token,
                                   lease_until=timestamp + timedelta(seconds=LEASE_SECONDS),
                                   attempts=Job.attempts + 1).returning(Job)).first()


def snapshot_alert(session, mode, offer, assessment):
    accepted, selection = selection_for(session, mode, offer, assessment)
    if not accepted:
        return
    minimum = dec(selection["min_profit"]) if selection else None
    if minimum is None:
        minimum = Decimal(os.getenv("TRACKER_MIN_PROFIT", "25"))
    save_signal(session, mode, offer, assessment, selection, minimum)
    if assessment["status"] != "available" or not assessment["estimates"]:
        return
    if utc(offer.effective_at) < now() - timedelta(hours=24):
        return
    best = assessment["estimates"][0]
    value = best["margin"]
    if not value or dec(value["profit"]) < minimum or best["confidence"] != "buona":
        return
    if selection and selection["max_cost"] is not None and dec(value["purchase"]) > dec(selection["max_cost"]):
        return
    has_point_check = mode == "real" and offer.source == "ebay" and direct_connection("ebay") == "Browse API"
    if has_point_check and (offer.data.get("checked_by") != "detail" or
                            utc(offer.effective_at) < now() - timedelta(minutes=30)):
        bucket = int(now().timestamp()) // 1800
        enqueue(session, mode, "refresh", f"refresh:alert:{offer.id}:{bucket}",
                {"source": offer.source, "id": offer.external_id})
        return
    alert_id = f"{offer.external_id}:{best['channel']}:{value['purchase']}"
    previous = events(session, mode, "alert", source=offer.source, external_id=alert_id)
    cooldown = timedelta(hours=int(os.getenv("TRACKER_ALERT_COOLDOWN_HOURS", "24")))
    if previous and utc(previous[-1].known_at) > now() - cooldown:
        return
    alert = {"title": offer.data["title"], "channel": best["channel"], "profit": value["profit"],
             "purchase": value["purchase"], "snapshot": assessment}
    window = int(now().timestamp() // max(1, cooldown.total_seconds()))
    identifier = record(session, mode, "alert", offer.source, alert_id, alert, identity=f"profit:{minimum}:{window}")
    if (identifier and mode == "real" and os.getenv("TRACKER_TELEGRAM_ENABLED") == "1"
            and os.getenv("TELEGRAM_BOT_TOKEN") and os.getenv("TELEGRAM_CHAT_ID")):
        enqueue(session, mode, "telegram", f"telegram:{identifier}", {"alert": identifier})


def snapshot_request_alerts(session, mode, offer, assessment):
    from .operations import duplicate_groups, reference, request_matches

    if assessment["status"] != "available" or utc(offer.effective_at) < now() - timedelta(hours=24):
        return
    ref = reference(offer)
    group = duplicate_groups(session, mode).get(ref, ref)
    for item in request_matches(session, mode, [{"offer": offer, "assessment": assessment, "group": group}]):
        request = item["request"]
        if request.data["opt_in"] and item["offers"]:
            record(session, mode, "request_alert", offer.source,
                   f"{request.external_id}:{group}"[:240],
                   {"title": offer.data["title"], "offer_id": offer.id,
                    "channel": offer.source, "purchase": assessment["purchase"]},
                   identity=f"{assessment['purchase']}")


def execute(session, job, client):
    if job.kind in {"discover", "refresh", "mail"}:
        collect(session, job.mode, job, client)
    elif job.kind == "evaluate":
        found = [row for row in events(session, job.mode, "offer", source=job.data["source"], external_id=job.data["id"])]
        if found:
            offer = found[-1]
            _, assessment = save_assessment(session, job.mode, offer)
            snapshot_alert(session, job.mode, offer, assessment)
            snapshot_request_alerts(session, job.mode, offer, assessment)
    elif job.kind == "comparables":
        from .catalog import catalog, match
        from .value import identification
        sale = session.get(Event, job.data["event_id"])
        products = catalog(session, job.mode)
        variant = match(sale.data, products)["product_id"]
        if variant:
            chip = products[variant]["attrs"].get("chip")
            for offer in latest(session, job.mode, "offer"):
                candidate = identification(session, job.mode, offer, products)["product_id"]
                if candidate == variant or (candidate and chip and products[candidate]["attrs"].get("chip") == chip):
                    enqueue(session, job.mode, "evaluate", f"evaluate:sale:{sale.id}:{offer.id}",
                            {"source": offer.source, "id": offer.external_id})
    else:
        raise FetchError("Tipo di job sconosciuto", retry=False)


def send_telegram(sessions, job, client):
    with sessions.begin() as session:
        active = session.get(Job, job.id, with_for_update=True)
        if active.lease_token != job.lease_token or active.status != "running":
            return
        if os.getenv("TRACKER_TELEGRAM_ENABLED") != "1":
            active.status = "cancelled"
            active.error = None
            return
        token = os.getenv("TELEGRAM_BOT_TOKEN")
        chat = os.getenv("TELEGRAM_CHAT_ID")
        alert = session.get(Event, job.data["alert"])
        if not token or not chat or alert is None:
            active.status = "failed"
            active.error = "Telegram o avviso non configurato"
            return
        active.status = "uncertain"
        active.error = "Invio in corso o esito sconosciuto. Verifica il canale."
        text = f"{alert.data['title']} · Margine stimato €{alert.data['profit']} · {alert.data['channel']}"
    try:
        response = client.post(f"https://api.telegram.org/bot{token}/sendMessage", json={"chat_id": chat, "text": text})
        success = response.is_success and response.json().get("ok") is True
    except (httpx.HTTPError, ValueError):
        return
    with sessions.begin() as session:
        active = session.get(Job, job.id, with_for_update=True)
        if success:
            active.status, active.error = "done", None
        elif 400 <= response.status_code < 500:
            active.status, active.error = "failed", f"Invio rifiutato da Telegram HTTP {response.status_code}"


def one(sessions, mode, client):
    with sessions.begin() as session:
        job = claim(session, mode)
    if not job:
        return False
    if job.kind == "telegram":
        send_telegram(sessions, job, client)
        return True
    try:
        with sessions.begin() as session:
            active = session.get(Job, job.id, with_for_update=True)
            if active.lease_token != job.lease_token or active.status != "running":
                return True
            execute(session, active, client)
            active.status, active.error = "done", None
            active.lease_until = None
    except Exception as error:
        with sessions.begin() as session:
            active = session.get(Job, job.id, with_for_update=True)
            if active.lease_token != job.lease_token:
                return True
            retry = error.retry if isinstance(error, FetchError) else True
            delay = max(min(900, 5 * 2 ** (active.attempts - 1)), getattr(error, "delay", 0))
            active.status = "retry" if retry and active.attempts < ATTEMPTS else "failed"
            active.error = str(error)[:240] if isinstance(error, FetchError) else type(error).__name__
            active.run_at = now() + timedelta(seconds=delay)
            active.lease_until = None
            if active.kind in {"discover", "refresh", "mail"}:
                record(session, mode, "collection_error", active.data["source"], f"{active.id}:{active.attempts}",
                       {"status": active.status, "message": active.error, "kind": active.kind})
                if active.kind == "refresh" and active.status == "failed":
                    entry = events(session, mode, "offer", source=active.data["source"], external_id=active.data["id"])
                    if entry:
                        timestamp = now()
                        identifier = record(session, mode, "offer", entry[-1].source, entry[-1].external_id,
                                            {**entry[-1].data, "status": "unknown"}, identity=f"error:{active.id}")
                        if identifier:
                            enqueue(session, mode, "evaluate", f"evaluate:offer:{identifier}",
                                    {"source": entry[-1].source, "id": entry[-1].external_id})
                        enqueue(session, mode, "refresh", f"refresh:after:{active.id}", active.data,
                                timestamp + timedelta(hours=12))
    return True


def run(sessions, modes=("demo", "real"), *, once=False):
    with httpx.Client(timeout=12, follow_redirects=False) as client:
        while True:
            for mode in modes:
                if mode == "real":
                    from .codex_index import synchronize
                    synchronize(sessions)
                with sessions.begin() as session:
                    schedule(session, mode)
                for _ in range(100):
                    if not one(sessions, mode, client):
                        break
            if once:
                return
            time.sleep(4)
