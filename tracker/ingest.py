import csv
import io
import json
import re
from datetime import datetime
from decimal import Decimal, InvalidOperation
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field, field_validator

from .db import now
from .store import digest, enqueue, events, record, schedule_offer, utc

ROME = ZoneInfo("Europe/Rome")


def amount(value):
    raw = str(value or "").strip().replace("€", "").replace(" ", "").replace("\u00a0", "")
    if isinstance(value, (int, float, Decimal)) and not isinstance(value, bool):
        raw = str(value)
    if not raw:
        return None
    if "," in raw and "." in raw:
        separator = "," if raw.rindex(",") > raw.rindex(".") else "."
        raw = raw.replace("." if separator == "," else ",", "").replace(separator, ".")
    else:
        raw = raw.replace(",", ".")
    try:
        number = Decimal(raw)
        if number.is_finite() and Decimal(0) <= number <= Decimal("1000000000"):
            return str(number.normalize()) if number == 0 else format(number.normalize(), "f")
    except InvalidOperation:
        pass
    raise ValueError("Importo non valido")


def date(value):
    if not value:
        return None
    try:
        result = datetime.fromisoformat(str(value))
    except ValueError:
        for fmt in ("%d/%m/%Y %H:%M", "%d/%m/%Y", "%d.%m.%Y"):
            try:
                result = datetime.strptime(str(value), fmt)
                break
            except ValueError:
                continue
        else:
            raise ValueError("Data non valida")
    return utc(result if result.tzinfo else result.replace(tzinfo=ROME))


def link(value):
    value = str(value or "").strip()
    if not value:
        return ""
    parsed = urlsplit(value)
    if parsed.scheme not in {"https", "http"} or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("URL HTTP(S) non valido")
    return value[:2048]


CONDITIONS = {"usato": "used", "used": "used", "nuovo": "new", "new": "new",
              "ricondizionato": "refurbished", "refurbished": "refurbished",
              "guasto": "broken", "broken": "broken", "unknown": "unknown", "": "unknown"}
AVAILABILITY = {"available": "available", "disponibile": "available", "active": "available",
                "unavailable": "unavailable", "non disponibile": "unavailable",
                "unknown": "unknown", "incerto": "unknown", "": "unknown"}


class InputRow(BaseModel):
    external_id: str = Field(min_length=1, max_length=180)
    title: str = Field(default="", max_length=512)
    description: str = Field(default="", max_length=5000)
    gtin: str = Field(default="", max_length=50)
    mpn: str = Field(default="", max_length=120)
    condition: str = "unknown"
    market: str = Field(default="IT", pattern=r"^[A-Z]{2}$")
    currency: str = Field(default="EUR", pattern=r"^[A-Z]{3}$")
    price: str | None = None
    shipping: str | None = None
    paid_price: str | None = None
    shown_price: str | None = None
    fx_rate: str | None = None
    fx_date: str = ""
    fx_source: str = ""
    quantity: int | None = Field(default=None, ge=0, le=100000)
    url: str = ""
    image_url: str = ""
    status: str = "unknown"
    sale_format: str = ""
    verified: bool = False
    reason: str = ""
    observed_at: str = ""
    sold_at: str = ""
    published_at: str = ""
    decision_at: str = ""
    decision_title: str = Field(default="", max_length=512)
    decision_condition: str = ""
    decision_format: str = ""
    order_id: str = Field(default="", max_length=120)
    listing_id: str = Field(default="", max_length=180)
    destination: str = ""
    area: str = Field(default="", max_length=100)
    photo_hash: str = Field(default="", max_length=64)
    origin_ref: str = Field(default="", max_length=180)
    checked_by: str = Field(default="", max_length=30)

    @field_validator("price", "shipping", "paid_price", "shown_price", "fx_rate", mode="before")
    @classmethod
    def money(cls, value):
        return amount(value)

    @field_validator("url", "image_url", mode="before")
    @classmethod
    def safe_url(cls, value):
        return link(value)

    @field_validator("verified", mode="before")
    @classmethod
    def truth(cls, value):
        if isinstance(value, str):
            return value.strip().lower() in {"true", "1", "yes", "sì", "si"}
        return bool(value)


EXPECTED = set(InputRow.model_fields)
TRANSACTION_FIELDS = {
    "title", "gtin", "mpn", "condition", "market", "currency", "paid_price", "shown_price", "shipping",
    "quantity", "status", "sale_format", "verified", "fx_rate", "fx_date", "fx_source", "order_id",
    "sold_at", "published_at", "decision_at", "destination", "listing_id",
    "decision_title", "decision_condition", "decision_format",
}
SOLD = {"sold", "paid", "venduto", "pagato", "completed"}
UNPAID = {"refunded", "cancelled", "returned", "reso", "annullato", "unpaid", "pending"}
EMAIL = re.compile(r"\b[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}\b")
PHONE = re.compile(r"(?<!\w)(?:\+39[\s.-]*)?(?:\d[\s.-]*){9,12}(?!\w)")


def clean_text(value):
    return PHONE.sub("[contatto omesso]", EMAIL.sub("[contatto omesso]", value))


def parse_file(raw, filename):
    text = raw.decode("utf-8-sig", errors="strict")
    if filename.lower().endswith(".json"):
        data = json.loads(text, parse_float=Decimal)
        if isinstance(data, dict):
            data = data.get("items")
        if not isinstance(data, list) or any(not isinstance(row, dict) for row in data):
            raise ValueError("JSON atteso come elenco di oggetti o oggetto con items")
        return [(index, row) for index, row in enumerate(data, start=1)]
    try:
        dialect = csv.Sniffer().sniff(text[:8192], delimiters=";,\t")
    except csv.Error:
        dialect = "excel"
    reader = csv.DictReader(io.StringIO(text), dialect=dialect)
    return [(index, row) for index, row in enumerate(reader, start=2)]


def normalize(row, mapping, kind):
    raw = {}
    columns = {str(key).strip().casefold(): value for key, value in row.items()}
    for name in EXPECTED:
        column = str(mapping.get(name, name)).strip().casefold()
        value = columns.get(column)
        if value is not None and value != "":
            raw[name] = value.strip() if isinstance(value, str) else value
    for name in ("market", "currency", "destination"):
        if name in raw:
            raw[name] = str(raw[name]).upper()
    data = InputRow.model_validate(raw).model_dump()
    observed = date(data.pop("observed_at"))
    checked = now()
    if observed and observed > checked:
        raise ValueError("Data di osservazione futura")
    state = data["status"].casefold()
    data["condition"] = CONDITIONS.get(data["condition"].casefold(), "unknown")
    data["title"] = clean_text(data["title"])
    data["description"] = clean_text(data["description"])
    data["decision_title"] = clean_text(data["decision_title"])
    data["decision_condition"] = CONDITIONS.get(data["decision_condition"].casefold(), "unknown")
    if data["fx_rate"] is not None:
        if Decimal(data["fx_rate"]) <= 0 or not data["fx_source"] or not date(data["fx_date"]):
            raise ValueError("Il cambio richiede tasso positivo, data e fonte")
        data["fx_date"] = date(data["fx_date"]).date().isoformat()
    for field in ("sold_at", "decision_at", "published_at"):
        parsed = date(data[field])
        data[field] = parsed.isoformat() if parsed else ""
    if kind == "sale":
        sold = date(data["sold_at"])
        if not sold or sold > (observed or checked):
            raise ValueError("La vendita richiede una data valida non successiva alla prima osservazione")
        if not data["quantity"]:
            raise ValueError("La vendita richiede una quantità positiva")
        data["status"] = "sold" if state in SOLD else "excluded" if state in UNPAID else "unknown"
        data["reason"] = (
            "Esito non verificato" if not data["verified"] else
            "Importo finale sconosciuto" if data["paid_price"] is None else ""
        )
        data = {key: value for key, value in data.items() if key in TRANSACTION_FIELDS | {"external_id", "reason"}}
        return data, sold, observed or checked, observed
    data["status"] = AVAILABILITY.get(state, "unknown")
    data["published_at"] = data["published_at"] or ""
    if kind == "stock" and not data["title"]:
        raise ValueError("Lo stock richiede un titolo")
    if kind == "offer" and not data["title"]:
        raise ValueError("L’offerta richiede un titolo")
    return data, observed or checked, observed or checked, observed


def preview(rows, mapping, kind):
    valid, errors = [], []
    for number, row in rows:
        try:
            values = normalize(row, mapping, kind)
            valid.append((number, values))
        except (ValueError, TypeError) as error:
            if hasattr(error, "errors"):
                reason = ", ".join(".".join(str(part) for part in entry["loc"]) + ": " + entry["msg"]
                                   for entry in error.errors())
            else:
                reason = str(error)
            errors.append({"row": number, "message": reason[:300]})
    return valid, errors


def write_rows(session, mode, source, kind, valid, errors, batch, *, refresh=False):
    count = 0
    for number, (data, effective, known, historical) in valid:
        data = dict(data)
        identifier = data.pop("external_id")
        if kind == "stock":
            identifier = f"{batch[:20]}:{identifier}"
            data.update(batch=batch[:20], line=number)
        identity = f"{historical.isoformat()}:{digest(json.dumps(data, sort_keys=True))}" if historical else None
        if kind == "sale":
            identity = None
        elif not historical:
            identity = f"{batch}:{number}"
        event_id = record(session, mode, kind, source, identifier, data, effective=effective, known=known, identity=identity)
        if event_id is None:
            continue
        count += 1
        if kind == "offer":
            schedule_offer(session, mode, source, identifier, event_id, refresh=refresh)
        elif kind == "sale":
            enqueue(session, mode, "comparables", f"comparables:{event_id}", {"event_id": event_id})
    for error in errors:
        record(session, mode, "import_error", source, f"{batch[:32]}:{error['row']}",
               {"batch": batch[:32], **error})
    if not events(session, mode, "import", source=source, external_id=batch):
        record(session, mode, "import", source, batch,
               {"kind": kind, "accepted": count, "rejected": len(errors), "rows": len(valid) + len(errors)})
    return count
