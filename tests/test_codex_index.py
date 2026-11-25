import json
from datetime import timedelta

from tracker.catalog import seed_catalog
from tracker.channels import set_source
from tracker.codex_index import synchronize
from tracker.db import now
from tracker.monitoring import save_watch, set_enabled
from tracker.store import events


def test_codex_manifests_and_results_follow_selected_products(sessions, monkeypatch, tmp_path):
    monkeypatch.setenv("TRACKER_CODEX_DISCOVERY", "1")
    monkeypatch.setenv("TRACKER_FEED_SUBITO_URL", "https://feed.example.test/subito")
    with sessions.begin() as session:
        seed_catalog(session, "real")
        set_source(session, "real", "facebook", history=False)
        save_watch(session, "real", "switch-oled", ("subito", "vinted", "facebook"), 15, signals=True)
    clock = now()
    synchronize(sessions, tmp_path, clock)
    manifest = tmp_path / "requests.json"
    [request] = json.loads(manifest.read_text(encoding="utf-8"))["items"]
    assert request["source"] == "vinted"
    title = "Console Nintendo Switch – Modello OLED, Bianco | Vinted"
    result = tmp_path / "results" / f"{request['id']}.json"
    result.write_text(json.dumps([
        {"title": title, "url": "https://www.vinted.it/items/9471196434-nintendo-switch-oled?ref=sample"},
        {"title": title, "url": "https://vinted.it.example.test/items/321-nintendo-switch-oled"},
    ]), encoding="utf-8")
    synchronize(sessions, tmp_path, clock + timedelta(minutes=15))
    assert not result.exists()
    with sessions() as session:
        [indexed] = events(session, "real", "index_result")
        assert indexed.data["provider"] == "codex_web_search"
        [alert] = events(session, "real", "monitor_signal")
        assert alert.data["price"] is None
        assert alert.data["url"] == "https://www.vinted.it/items/9471196434-nintendo-switch-oled"
        [collection] = events(session, "real", "collection")
        assert collection.data["matched"] == 1
        assert not events(session, "real", "offer")
    [next_request] = json.loads(manifest.read_text(encoding="utf-8"))["items"]
    assert next_request["id"] != request["id"]
    next_result = tmp_path / "results" / f"{next_request['id']}.json"
    next_result.write_text(json.dumps([{"title": title, "url": "https://www.vinted.it/items/5670-switch"}]), encoding="utf-8")
    with sessions.begin() as session:
        set_enabled(session, "real", "switch-oled", False)
    synchronize(sessions, tmp_path, clock + timedelta(minutes=15))
    assert json.loads(manifest.read_text(encoding="utf-8")) == {"items": []}
    with sessions() as session:
        assert len(events(session, "real", "monitor_signal")) == 1
