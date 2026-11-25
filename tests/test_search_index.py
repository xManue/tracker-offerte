from datetime import timedelta

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from tracker.catalog import seed_catalog
from tracker.channels import set_source
from tracker.connectors import connections, schedule
from tracker.db import Job, now
from tracker.monitoring import save_watch
from tracker.store import events
from tracker.web import create_app
from tracker.worker import one


def enable_index(monkeypatch):
    monkeypatch.setenv("TRACKER_BRAVE_API_KEY", "test-search-token")
    monkeypatch.setenv("TRACKER_BRAVE_SOURCES", "vinted,wallapop,facebook")
    monkeypatch.setenv("TRACKER_BRAVE_STORAGE_ALLOWED", "1")
    monkeypatch.setenv("TRACKER_BRAVE_MAX_PAGES", "2")


@pytest.mark.parametrize(("source", "scope", "result_url", "publisher"), [
    ("vinted", "nintendo switch oled site:vinted.it",
     "https://www.vinted.it/items/9471196434-nintendo-switch-oled", "Vinted"),
    ("wallapop", "nintendo switch oled site:wallapop.com",
     "https://it.wallapop.com/item/nintendo-switch-oled-9471196434", "Wallapop"),
    ("facebook", "nintendo switch oled marketplace site:facebook.com",
     "https://www.facebook.com/marketplace/item/9471196434/", "Facebook Marketplace"),
])
def test_indexed_discovery_repeats_and_alerts_without_creating_offers(
        sessions, monkeypatch, source, scope, result_url, publisher):
    enable_index(monkeypatch)
    calls = []

    def answer(request):
        assert request.url.host == "api.search.brave.com"
        assert request.headers["X-Subscription-Token"] == "test-search-token"
        assert request.url.params["country"] == "IT"
        assert request.url.params["q"] == scope
        offset = request.url.params["offset"]
        calls.append(offset)
        return httpx.Response(200, json={"query": {"more_results_available": True}, "web": {"results": [
            {"url": result_url + "?tracking=abc", "title": f"Nintendo Switch OLED | {publisher}", "price": "150"},
            {"url": result_url, "title": "Nintendo Switch OLED"},
            {"url": result_url.replace("9471196434", "123456"), "title": "Nintendo Switch Lite"},
            {"url": "https://www.vinted.it/catalog?search_text=switch", "title": "Nintendo Switch OLED"},
            {"url": "https://www.vinted.it.example.test/items/654321-switch", "title": "Nintendo Switch OLED"},
        ]}})

    clock = now()
    with sessions.begin() as session:
        seed_catalog(session, "real")
        set_source(session, "real", source, history=True)
        save_watch(session, "real", "switch-oled", (source,), 15, signals=True)
        schedule(session, "real", clock)
        schedule(session, "real", clock)
        assert len(list(session.scalars(select(Job)))) == 1
    with httpx.Client(transport=httpx.MockTransport(answer)) as client:
        assert one(sessions, "real", client)
        with sessions.begin() as session:
            follow_up = session.scalar(select(Job).where(Job.status == "pending"))
            follow_up.run_at = now() - timedelta(seconds=1)
        assert one(sessions, "real", client)
        assert not one(sessions, "real", client)
        with sessions.begin() as session:
            schedule(session, "real", clock + timedelta(minutes=15))
        assert one(sessions, "real", client)
    assert calls == ["0", "1", "0"]
    with sessions() as session:
        [result] = events(session, "real", "index_result")
        assert result.data["url"] == result_url
        [signal] = events(session, "real", "monitor_signal")
        assert signal.data["purchase"] is None and signal.data["price"] is None
        assert not events(session, "real", "offer") and not events(session, "real", "alert")
        assert len(events(session, "real", "collection")) == 3
    with TestClient(create_app(sessions)) as browser:
        alerts = browser.get("/real/alerts").text
        assert result_url in alerts and "risultato indicizzato" in alerts
        assert browser.get("/real/sources").status_code == 200


def test_index_requires_source_permission_storage_and_signal_opt_in(sessions, monkeypatch):
    enable_index(monkeypatch)
    monkeypatch.setenv("TRACKER_BRAVE_STORAGE_ALLOWED", "0")
    assert connections("vinted") == []
    with sessions.begin() as session:
        seed_catalog(session, "real")
        set_source(session, "real", "wallapop", history=True)
        save_watch(session, "real", "switch-oled", ("wallapop", "facebook"), 15, signals=True)
        schedule(session, "real")
        assert session.scalar(select(Job)) is None
        monkeypatch.setenv("TRACKER_BRAVE_STORAGE_ALLOWED", "1")
        assert connections("wallapop") == ["Indice Brave"]
        save_watch(session, "real", "switch-oled", ("wallapop", "facebook"), 15, signals=False)
        schedule(session, "real")
        assert session.scalar(select(Job)) is None
        save_watch(session, "real", "switch-oled", ("wallapop", "facebook"), 15, signals=True)
        schedule(session, "real")
        assert [job.data["source"] for job in session.scalars(select(Job))] == ["wallapop"]


def test_direct_feed_takes_precedence_over_index(sessions, monkeypatch):
    enable_index(monkeypatch)
    monkeypatch.setenv("TRACKER_FEED_VINTED_URL", "https://feed.example.test/vinted")
    assert connections("vinted") == ["Feed JSON"]

    def answer(request):
        assert request.url.host == "feed.example.test"
        return httpx.Response(200, json={"items": [
            {"external_id": "123", "title": "Nintendo Switch OLED", "price": "215",
             "condition": "used", "status": "available"},
        ]})

    with sessions.begin() as session:
        seed_catalog(session, "real")
        set_source(session, "real", "vinted", history=True)
        save_watch(session, "real", "switch-oled", ("vinted",), 15, signals=True)
        schedule(session, "real")
    with httpx.Client(transport=httpx.MockTransport(answer)) as client:
        assert one(sessions, "real", client)
    with sessions() as session:
        assert len(events(session, "real", "offer")) == 1
        assert not events(session, "real", "index_result")
