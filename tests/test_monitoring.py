from datetime import timedelta
from email.message import EmailMessage
from email.utils import format_datetime
import hashlib
import hmac
import json
from urllib.parse import parse_qs, urlsplit

import httpx
from fastapi.testclient import TestClient
from sqlalchemy import select

from tracker.catalog import catalog, match, seed_catalog
from tracker.channels import set_source
from tracker.connectors import adapter, connections, schedule
from tracker.db import Job, now
from tracker.mail_alerts import canonical_listing, fetch_messages, MailConfig, parse_message
from tracker.monitoring import save_watch, set_enabled, watches
from tracker.store import events, latest
from tracker.web import create_app
from tracker.worker import one

MONITORED = "rtx4070-asus-dual"
GPU = "ASUS Dual GeForce RTX 4070 12 GB"
SENDER = "alerts@subito.it"


def test_existing_and_new_catalog_models_match_without_merging_variants(sessions):
    with sessions.begin() as session:
        seed_catalog(session, "real")
        products = catalog(session, "real")
        assert len(products) >= 19
        assert match({"title": "Sapphire Radeon RX 7800XT 16 GB"}, products)["product_id"] == "rx7800xt"
        assert match({"title": "MSI RTX 4060 Ti 8 GB"}, products)["product_id"] is None
        assert match({"title": "RTX 3060 8 GB"}, products)["product_id"] is None
        assert match({"title": "Sony PS5 Slim Digital Edition"}, products)["product_id"] is None
        assert match({"title": "Console Nintendo Switch – Modello OLED, Bianco"}, products)["product_id"] == "switch-oled"
        assert match({"title": "Nintendo Switch 2 Modello OLED"}, products)["product_id"] is None
        assert match({"title": "Solo scatola Nintendo Switch Modello OLED"}, products)["product_id"] is None
        assert match({"title": "Set LEGO 75375 Millennium Falcon"}, products)["product_id"] == "lego-75375"
        assert match({"title": "Makita DHP484Z corpo macchina"}, products)["product_id"] == "makita-dhp484"


def test_worker_repeats_selected_marketplaces_and_pauses_existing_jobs(sessions, monkeypatch):
    for source in ("SUBITO", "VINTED", "AMAZON"):
        monkeypatch.setenv(f"TRACKER_FEED_{source}_URL", f"https://fixture.test/{source.casefold()}")
    calls = []

    def respond(request):
        source = request.url.path.split("/")[-1]
        calls.append(source)
        assert request.url.params["q"] == "RTX 4070 ASUS Dual"
        return httpx.Response(200, json={"items": [
            {"external_id": source + "-target", "title": GPU, "price": "225",
             "status": "available", "condition": "used"},
            {"external_id": source + "-over", "title": GPU, "price": "375",
             "status": "available", "condition": "used"},
            {"external_id": source + "-other", "title": "Nintendo Switch OLED", "price": "200",
             "status": "available", "condition": "used"},
        ]})

    clock = now()
    with sessions.begin() as session:
        seed_catalog(session, "real")
        for source in ("subito", "vinted", "amazon"):
            set_source(session, "real", source, history=source != "amazon")
        save_watch(session, "real", MONITORED, ("subito", "vinted", "amazon"), 15,
                   query="RTX 4070 ASUS Dual", max_cost="270", signals=True)
        schedule(session, "real", clock)
        schedule(session, "real", clock)
        assert len(list(session.scalars(select(Job)))) == 2
    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        while one(sessions, "real", client):
            pass
        with sessions.begin() as session:
            assert sorted(calls) == ["subito", "vinted"]
            assert {row.external_id for row in events(session, "real", "monitor_signal")} == {"subito-target", "vinted-target"}
            assert not events(session, "real", "alert")
            schedule(session, "real", clock + timedelta(minutes=15))
            assert len(list(session.scalars(select(Job).where(Job.status == "pending")))) == 2
            set_enabled(session, "real", MONITORED, False)
        while one(sessions, "real", client):
            pass
    assert sorted(calls) == ["subito", "vinted"]


def message_at(timestamp, sender=SENDER):
    message = EmailMessage()
    message["From"] = sender
    message["To"] = "private@example.test"
    message["Date"] = format_datetime(timestamp)
    message["Subject"] = "Aggiornamento ricerca salvata"
    message.set_content(f"{GPU} 245 € https://www.subito.it/informatica/rtx-4070-753459.htm?tracking=xyz")
    message.add_alternative(
        f'<table><tr><td><a href="https://www.subito.it/informatica/rtx-4070-753459.htm?tracking=xyz">{GPU}</a>'
        '<br><span>245 €</span></td></tr></table>'
        '<a href="https://subito.it.example.test/informatica/altro-999999.htm">Rifiuta</a>',
        subtype="html",
    )
    return message.as_bytes()


def test_mail_to_selected_products_and_alerts_without_saved_raw_mail(sessions, monkeypatch):
    for key, value in {"HOST": "imap.fixture.test", "USER": "private@example.test", "PASSWORD": "fixture",
                       "FOLDER": "Tracker/Subito", "SENDER": SENDER}.items():
        monkeypatch.setenv("TRACKER_MAIL_SUBITO_" + key, value)
    received = now() - timedelta(minutes=2)
    raw = message_at(received)
    seen = []

    def mailbox(config, checkpoint):
        seen.append(checkpoint)
        if checkpoint:
            return "77", 41, [], False
        return "77", 41, [(40, message_at(received, "other@fixture.test")), (41, raw)], False

    monkeypatch.setattr("tracker.connectors.fetch_messages", mailbox)
    with sessions.begin() as session:
        seed_catalog(session, "real")
        set_source(session, "real", "subito", history=True)
        save_watch(session, "real", MONITORED, ("subito",), 60, signals=True)
        save_watch(session, "real", "switch-oled", ("subito",), 120)
        schedule(session, "real", received)
        assert session.scalar(select(Job.kind)) == "mail"
    with httpx.Client(transport=httpx.MockTransport(lambda request: pytest_fail(request))) as client:
        while one(sessions, "real", client):
            pass
        with sessions.begin() as session:
            schedule(session, "real", received + timedelta(hours=1))
        while one(sessions, "real", client):
            pass
    assert seen == [{}, {"validity": "77", "uid": 41}]
    with sessions() as session:
        [offer] = latest(session, "real", "offer")
        assert offer.external_id == "753459"
        assert offer.data["status"] == "unknown"
        assert offer.data["price"] == "245"
        assert "?" not in offer.data["url"]
        assert all("private@example.test" not in str(item.data) for item in events(session, "real", "mail_cursor"))
    with TestClient(create_app(sessions)) as browser:
        assert browser.get("/real/sources").status_code == 200
        response = browser.get("/real/alerts")
        assert "Offerte da verificare" in response.text and GPU in response.text
        assert "disponibilità da confermare" in response.text
        toggle = f"/real/sources/products/{MONITORED}/state"
        assert browser.post(toggle, data={"enabled": "false"}).status_code == 200
        assert "In pausa" in browser.get("/real/sources").text


def pytest_fail(request):
    raise AssertionError(f"Richiesta HTTP inattesa: {urlsplit(str(request.url)).hostname}")


def test_mailbox_reads_uid_and_leave_messages_unread(monkeypatch):
    operations = []
    raw = message_at(now())

    class Mailbox:
        def login(self, user, password):
            pass

        def select(self, folder, *, readonly):
            assert readonly and folder == '"Search/Subito"'
            return "OK", [b"2"]

        def response(self, name):
            assert name == "UIDVALIDITY"
            return name, [b"2"]

        def uid(self, command, *args):
            operations.append((command, args))
            if command == "SEARCH":
                return "OK", [b"13 14"]
            if args[1] == "(UID RFC822.SIZE)":
                return "OK", [b"1 (UID 13 RFC822.SIZE 800000)", b"2 (UID 14 RFC822.SIZE 800)"]
            assert args == ("14", "(UID BODY.PEEK[])")
            return "OK", [(b"2 (UID 14 BODY[] {800}", raw), b")"]

        def logout(self):
            pass

    monkeypatch.setattr("tracker.mail_alerts.imaplib.IMAP4_SSL", lambda *args, **kwargs: Mailbox())
    config = MailConfig("imap.fixture.test", "private", "fixture", "Search/Subito", SENDER)
    assert fetch_messages(config, {"uid": 12, "validity": "2"}) == ("2", 14, [(14, raw)], False)
    assert operations[0][1] == (None, "UID", "13:*", "FROM", f'"{SENDER}"')
    assert canonical_listing("vinted", "https://www.vinted.it/items/123-switch") == (
        "123", "https://www.vinted.it/items/123-switch")
    assert canonical_listing("subito", "https://www.subito.it:invalid/informatica/articolo-123.htm") is None
    assert not parse_message("subito", raw, "other@fixture.test", now())


def test_product_settings_from_browser(sessions):
    with sessions.begin() as session:
        seed_catalog(session, "real")
    with TestClient(create_app(sessions)) as browser:
        response = browser.post("/real/sources/products", data={"product_id": "switch-oled",
            "sources": ["subito", "vinted"], "minutes": "30", "max_cost": "220,50", "min_profit": "20", "signals": "true"})
        assert response.status_code == 200
        assert "Nintendo Switch OLED" in response.text and "Ogni 30 min" in response.text
        with sessions() as session:
            one_watch = watches(session, "real")["switch-oled"].data
            assert one_watch["max_cost"] == "220.5" and one_watch["min_profit"] == "20"


def test_amazon_italy_catalog_search_requires_approval_and_keeps_unknown_shipping(monkeypatch):
    for key in ("ID", "SECRET", "TAG"):
        monkeypatch.setenv("AMAZON_CREATORS_" + key, "fixture-" + key.lower())
    assert connections("amazon") == []
    monkeypatch.setenv("AMAZON_CREATORS_TRACKER_APPROVED", "1")
    assert connections("amazon") == ["Creators API"]
    received = []

    def respond(request):
        body = json.loads(request.content)
        received.append(request.url.path)
        if request.url.path == "/auth/o2/token":
            assert request.url.host == "api.amazon.co.uk"
            assert body["scope"] == "creatorsapi::default"
            return httpx.Response(200, json={"access_token": "mock-token", "expires_in": 3600})
        assert request.url.host == "creatorsapi.amazon"
        assert request.headers["Authorization"] == "Bearer mock-token"
        assert request.headers["x-marketplace"] == body["marketplace"] == "www.amazon.it"
        assert body["keywords"] == "Nintendo Switch OLED"
        listing = {"isBuyBoxWinner": True, "condition": {"value": "New"}, "availability": {"type": "IN_STOCK"},
                   "price": {"money": {"amount": 275.0, "currency": "EUR"}}}
        return httpx.Response(200, json={"searchResult": {"totalResultCount": 11, "items": [
            {"asin": "B012345678", "itemInfo": {"title": {"displayValue": "Nintendo Switch OLED"}},
             "detailPageURL": "https://www.amazon.it/dp/B012345678", "offersV2": {"listings": [listing]}}
        ]}})

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        amazon = adapter(client, "amazon")
        first = amazon.discover("Nintendo Switch OLED", None)
        second = amazon.discover("Nintendo Switch OLED", first.next_cursor)
    assert received.count("/auth/o2/token") == 1
    [product] = first.rows
    assert product["price"] == 275 and product["condition"] == "new" and "shipping" not in product
    assert first.next_cursor == "2" and second.next_cursor is None


def test_aliexpress_uses_italian_affiliate_query_and_observation_is_unconfirmed(monkeypatch):
    monkeypatch.setenv("ALIEXPRESS_TOP_KEY", "fixture-key")
    monkeypatch.setenv("ALIEXPRESS_TOP_SECRET", "fixture-secret")
    monkeypatch.setenv("ALIEXPRESS_TRACKING_ID", "fixture-tracking")
    assert connections("aliexpress") == ["Affiliate API"]

    def respond(request):
        assert request.url.host == "eco.taobao.com"
        params = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
        assert params["method"] == "aliexpress.affiliate.product.query"
        assert params["ship_to_country"] == "IT" and params["target_currency"] == "EUR"
        assert params["keywords"] == "Makita DHP484"
        received_sign = params.pop("sign")
        payload = "".join(key + params[key] for key in sorted(params)).encode()
        expected = hmac.new(b"fixture-secret", payload, hashlib.md5).hexdigest().upper()
        assert received_sign == expected
        return httpx.Response(200, json={"aliexpress_affiliate_product_query_response": {"resp_result": {
            "resp_code": 200, "result": {"total_page_no": 2, "products": {"product": [
                {"product_id": 4321, "product_title": "Makita DHP484", "target_sale_price": "55.90",
                 "target_sale_price_currency": "EUR", "product_detail_url": "https://www.aliexpress.com/item/4321.html"}
            ]}}}}})

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        page = adapter(client, "aliexpress").discover("Makita DHP484", None)
    [offer] = page.rows
    assert offer["price"] == "55.90" and offer["status"] == "unknown" and "shipping" not in offer
    assert page.next_cursor == "2"
