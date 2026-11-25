import io
from datetime import timedelta

import httpx
from fastapi.testclient import TestClient

from tracker.catalog import seed_catalog
from tracker.channels import set_source
from tracker.connectors import watch
from tracker.db import now
from tracker.demo import GPU, seed
from tracker.store import events, latest
from tracker.web import create_app
from tracker.worker import one, run
from tracker.connectors import schedule


def test_demo_pages_and_workflows(sessions):
    with sessions.begin() as session:
        seed(session)
    run(sessions, ("demo",), once=True)
    with TestClient(create_app(sessions)) as client:
        assert client.get("/").status_code == 200
        for path in ("", "/review", "/alerts", "/sources", "/lots", "/requests", "/budget", "/inventory", "/simulation"):
            response = client.get("/demo" + path)
            assert response.status_code == 200
            assert "Dati sintetici" in response.text
        assert client.get("/demo", params={"profit": "", "percent": "", "maximum": "", "evidence": "0"}).status_code == 200
        offers = client.get("/demo/api/offers").json()
        target = next(item for item in offers if item["source"] == "subito" and item["purchase"] == "259.00")
        detail = client.get(f"/demo/offers/{target['id']}")
        assert "Rivendita per canale" in detail.text and "Ripubblicazione" not in detail.text.split("<h1>")[1].split("</h1>")[0]
        generic = next(item for item in offers if "vecchio" in item["title"])
        url = f"/demo/offers/{generic['id']}"
        assert client.post(url + "/match", data={"product_id": "pc-am4-4070"}).status_code == 200
        assert client.post(url + "/decision", data={"state": "watch", "favorite": "true"}).status_code == 200
        assert "PC gaming vecchio" in client.get("/demo?favorite=true").text
        choices = [target] + [next(item for item in offers if item["title"].startswith(prefix))
                              for prefix in ("AMD Ryzen", "Kit B550", "Case mATX")]
        data = {"choices": [f"offer:{item['id']}" for item in choices], "title": "Piano prova", "labor": "0", "reserve": "true"}
        assert client.post("/demo/builds", data=data).status_code == 200
        assert client.post("/demo/builds", data=data).status_code == 400
        assert "Alimentatore · compatibile" in client.get("/demo/lots").text
        assert client.post("/demo/inventory", data={"title": "Pezzo test", "cost": "30", "offer_id": ""}).status_code == 200


def test_import_preview_to_dashboard(sessions):
    with sessions.begin() as session:
        seed_catalog(session, "real")
        set_source(session, "real", "local", history=True)
    raw = ("ID;Articolo;Prezzo;Spedizione;status;condition\n"
           f"sample;{GPU};250,00;9,00;available;used\n"
           "broken;;xx;xx;available;used\n").encode()
    with TestClient(create_app(sessions)) as client:
        response = client.post("/real/imports", data={"kind": "offer", "source": "local",
            "columns": '{"external_id":"ID","title":"Articolo","price":"Prezzo","shipping":"Spedizione"}'},
            files={"file": ("upload.csv", raw, "text/csv")})
        assert response.status_code == 200 and "1 righe valide" in response.text
        with sessions() as session:
            token = events(session, "real", "import_preview")[0].id
            assert not events(session, "real", "offer")
        assert "Riga;Motivo" in client.get(f"/real/imports/{token}/errors").text
        assert client.post(f"/real/imports/{token}").status_code == 200
        assert client.post(f"/real/imports/{token}").status_code == 200
        results = client.get("/real/api/offers").json()
        assert len(results) == 1 and results[0]["purchase"] == "259.00"
        assert client.get("/demo/api/offers").json() == []


def test_ebay_worker_against_mocked_browse_api(sessions, monkeypatch):
    monkeypatch.setenv("EBAY_CLIENT_ID", "fixture")
    monkeypatch.setenv("EBAY_CLIENT_SECRET", "fixture")
    with sessions.begin() as session:
        seed_catalog(session, "real")
        set_source(session, "real", "ebay", history=True)
        watch(session, "real", "RTX 4070", "ebay")
        schedule(session, "real")
    calls = []

    def respond(request):
        calls.append(request.url.path)
        if request.url.path.endswith("/token"):
            return httpx.Response(200, json={"access_token": "fixture", "expires_in": 3600})
        assert request.headers["X-EBAY-C-MARKETPLACE-ID"] == "EBAY_IT"
        assert request.url.params["q"] == "RTX 4070"
        return httpx.Response(200, json={"itemSummaries": [{"itemId": "v1|123|0", "title": GPU,
            "price": {"value": "250", "currency": "EUR"}, "conditionId": "3000",
            "shippingOptions": [{"shippingCost": {"currency": "EUR", "value": "9"}}]}]})

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        assert one(sessions, "real", client)
    assert len(calls) == 2
    with sessions() as session:
        assert latest(session, "real", "offer")[0].data["shipping"] == "9"
        assert latest(session, "real", "sale") == []


def test_marketplace_inventory_sale_and_return_preserve_history(sessions):
    with sessions.begin() as session:
        seed(session)
        item = next(row for row in latest(session, "demo", "inventory") if row.external_id == "demo-inventory")
    with TestClient(create_app(sessions)) as client:
        path = f"/demo/inventory/{item.id}"
        assert client.post(path, data={"state": "listed", "channel": "subito", "price": "419"}).status_code == 200
        assert client.post(path, data={"state": "sold", "channel": "subito", "price": "400",
                                      "fees": "10", "outbound": "8", "extras": "2"}).status_code == 200
        with sessions() as session:
            finished = latest(session, "demo", "inventory")[0]
            sale = events(session, "demo", "sale", source="local")[0]
            assert finished.data["realized_profit"] == "120.00"
            assert sale.data["destination"] == "SUBITO"
            assert sale.data["decision_title"]
        assert client.post(path, data={"state": "returned"}).status_code == 200
        with sessions() as session:
            assert [row.data["status"] for row in events(session, "demo", "sale", source="local")] == ["sold", "excluded"]


def test_request_notifications_require_opt_in(sessions):
    with sessions.begin() as session:
        seed(session)
    data = {"product_id": "rtx4070-asus-dual", "budget": "285", "quantity": "1", "conditions": "used",
            "expires": (now() + timedelta(days=7)).date().isoformat()}
    with TestClient(create_app(sessions)) as client:
        assert client.post("/demo/requests", data=data).status_code == 200
        with sessions() as session:
            assert not events(session, "demo", "request_alert")
        assert client.post("/demo/requests", data={**data, "opt_in": "true"}).status_code == 200
        assert "Abbinamenti alle richieste" in client.get("/demo/alerts").text


def test_stock_xlsx_and_pdf_scan_keep_source_location(monkeypatch):
    from openpyxl import Workbook
    from PIL import Image
    from tracker import documents

    book = Workbook()
    book.active.append(("title", "quantity"))
    book.active.append(("GPU esempio", 2))
    spreadsheet = io.BytesIO()
    book.save(spreadsheet)
    [(_, row)] = documents.read_stock(spreadsheet.getvalue(), "stock.xlsx")
    assert row["quantity"] == 2 and row["origin_ref"].endswith("riga 2")

    scan = io.BytesIO()
    Image.new("RGB", (300, 100), "white").save(scan, format="PDF")

    def read_image(image, *, lines):
        assert image.width > 300 and lines
        return {"text": "CODICE;GPU esempio;2;usato;100"}

    monkeypatch.setattr(documents, "image_evidence", read_image)
    [(_, row)] = documents.read_stock(scan.getvalue(), "stock.pdf")
    assert row["origin_ref"] == "Pagina 1, riga 1 (OCR)"
    assert row["quantity"] == "2"
