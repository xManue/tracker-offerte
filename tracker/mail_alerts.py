import imaplib
import os
import re
import ssl
from contextlib import suppress
from dataclasses import dataclass
from datetime import timedelta
from email import policy
from email.parser import BytesParser
from email.utils import parseaddr, parsedate_to_datetime
from html.parser import HTMLParser
from urllib.parse import urlsplit

from .db import now
from .store import utc

LISTINGS = {
    "subito": (("subito.it",), r"-(\d+)\.htm$"),
    "vinted": (("vinted.it", "vinted.com"), r"^/items/(\d+)(?:-|/|$)"),
    "wallapop": (("wallapop.com",), r"^/item/[^/]*-(\d+)/?$"),
    "facebook": (("facebook.com",), r"^/marketplace/item/(\d+)(?:/|$)"),
    "ebay": (("ebay.it", "ebay.com"), r"^/itm/(?:[^/]+/)?(\d+)/?$"),
    "amazon": (("amazon.it",), r"/(?:dp|gp/product)/([A-Z0-9]{10})(?:/|$)"),
    "aliexpress": (("aliexpress.com",), r"^/item/(\d+)\.html$"),
}
PRICE = re.compile(r"(?:€\s*([0-9][0-9.,]*)|(?<!\w)([0-9][0-9.,]*)\s*€)")
URL = re.compile(r"https://[^\s<>]+")
GENERIC = {"annuncio", "apri annuncio", "guarda annuncio", "scopri di più", "vedi annuncio",
           "vedi su subito", "vai all’annuncio", "visualizza"}


@dataclass(frozen=True)
class MailConfig:
    host: str
    user: str
    password: str
    folder: str
    sender: str


class MailError(RuntimeError):
    def __init__(self, message, *, retry=True):
        super().__init__(message)
        self.retry = retry


def settings(source):
    if source not in LISTINGS:
        return None
    prefix = f"TRACKER_MAIL_{source.upper()}_"
    values = [os.getenv(prefix + key, "").strip() if key != "PASSWORD" else os.getenv(prefix + key, "")
              for key in ("HOST", "USER", "PASSWORD", "FOLDER", "SENDER")]
    return MailConfig(*values) if all(values) else None


def canonical_listing(source, value):
    try:
        parsed = urlsplit(value.strip().rstrip(".,)>"))
        host, port = parsed.hostname or "", parsed.port
    except ValueError:
        return None
    domains, expression = LISTINGS[source]
    if parsed.scheme != "https" or not any(host == domain or host.endswith("." + domain) for domain in domains):
        return None
    if parsed.username or parsed.password or port not in (None, 443):
        return None
    target = re.search(expression, parsed.path)
    if not target:
        return None
    return target.group(1), f"https://{host}{parsed.path}"


def visible(value):
    return " ".join(value.split())


class ListingLinks(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.links = []
        self.active = None
        self.scope = []
        self.serial = 0

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "tr":
            self.serial += 1
            self.scope.append({"id": self.serial, "text": []})
        if tag == "a" and attrs.get("href"):
            self.active = {"href": attrs["href"], "text": [], "alt": attrs.get("title", ""),
                           "scope": self.scope[-1] if self.scope else None}
        elif tag == "img" and self.active is not None and not self.active["alt"]:
            self.active["alt"] = attrs.get("alt", "")

    def handle_data(self, data):
        if self.active is not None:
            self.active["text"].append(data)
        for block in self.scope:
            block["text"].append(data)

    def handle_endtag(self, tag):
        if tag == "a" and self.active is not None:
            self.links.append(self.active)
            self.active = None
        if tag == "tr" and self.scope:
            self.scope.pop()


def price_in(text):
    found = PRICE.findall(text)
    values = {left or right for left, right in found}
    return next(iter(values)) if len(values) == 1 else None


def offer_row(identifier, url, title, price, observed):
    title = visible(PRICE.sub("", title)).strip(" –:-")
    if len(title) < 5 or title.casefold() in GENERIC:
        return None
    return {"external_id": identifier, "url": url, "title": title[:512], "price": price,
            "currency": "EUR", "condition": "unknown", "status": "unknown", "market": "IT",
            "checked_by": "mail", "observed_at": observed.isoformat()}


def rows_from_html(source, body, observed):
    parser = ListingLinks()
    parser.feed(body)
    links = []
    scopes = {}
    for link in parser.links:
        value = canonical_listing(source, link["href"])
        if value is None:
            continue
        identifier, url = value
        scope = link["scope"]
        if scope is not None:
            scopes.setdefault(scope["id"], set()).add(identifier)
        links.append((identifier, url, link))
    rows = {}
    for identifier, url, link in links:
        text = visible(" ".join(link["text"]))
        title = text if len(text) >= 5 and text.casefold() not in GENERIC else link["alt"]
        price = price_in(text)
        scope = link["scope"]
        if price is None and scope is not None and len(scopes[scope["id"]]) == 1:
            price = price_in(" ".join(scope["text"]))
        row = offer_row(identifier, url, title, price, observed)
        if row and (identifier not in rows or (rows[identifier]["price"] is None and price is not None)):
            rows[identifier] = row
    return rows


def rows_from_text(source, body, observed):
    rows = {}
    for line in body.splitlines():
        links = URL.findall(line)
        if len(links) != 1:
            continue
        value = canonical_listing(source, links[0])
        if value:
            text = URL.sub("", line)
            row = offer_row(*value, text, price_in(text), observed)
            if row:
                rows[row["external_id"]] = row
    return rows


def parse_message(source, raw, sender, received):
    if len(raw) > 512_000:
        return []
    message = BytesParser(policy=policy.default).parsebytes(raw)
    if parseaddr(str(message["From"] or ""))[1].casefold() != sender.casefold():
        return []
    try:
        observed = utc(parsedate_to_datetime(str(message["Date"])))
    except (TypeError, ValueError, OverflowError):
        observed = received
    if observed > received:
        observed = received
    if observed < received - timedelta(days=7):
        return []
    html = message.get_body(preferencelist=("html",))
    plain = message.get_body(preferencelist=("plain",))
    rows = rows_from_text(source, plain.get_content(), observed) if plain is not None else {}
    if html is not None:
        rows.update(rows_from_html(source, html.get_content(), observed))
    return list(rows.values())


def fetch_messages(config, checkpoint, *, batch_size=25):
    sender = config.sender.casefold()
    if not re.fullmatch(r"[a-z0-9.!#$%&'*+/=?^_{}|~-]+@[a-z0-9.-]+", sender):
        raise MailError("Mittente IMAP non valido", retry=False)
    if not config.folder.strip() or any(character in config.folder for character in '\r\n"\\'):
        raise MailError("Cartella IMAP non valida", retry=False)
    try:
        mailbox = imaplib.IMAP4_SSL(config.host, ssl_context=ssl.create_default_context(), timeout=12)
    except OSError as error:
        raise MailError("Connessione IMAP non riuscita") from error
    try:
        try:
            mailbox.login(config.user, config.password)
        except imaplib.IMAP4.error as error:
            raise MailError("Autenticazione IMAP non riuscita", retry=False) from error
        state, _ = mailbox.select(f'"{config.folder}"', readonly=True)
        if state != "OK":
            raise MailError("Cartella IMAP non accessibile", retry=False)
        _, validity = mailbox.response("UIDVALIDITY")
        if not validity or not validity[0] or not validity[0].isdigit():
            raise MailError("La casella IMAP non ha fornito UIDVALIDITY")
        validity = validity[0].decode("ascii")
        after = checkpoint.get("uid", 0) if checkpoint.get("validity") == validity else 0
        state, data = mailbox.uid("SEARCH", None, "UID", f"{after + 1}:*", "FROM", f'"{sender}"')
        if state != "OK":
            raise MailError("Ricerca messaggi IMAP non riuscita")
        eligible = sorted(int(uid) for uid in data[0].split() if uid.isdigit() and int(uid) > after)
        # Start from the recent tail on first use, then resume the most recent completed UID.
        if not after:
            eligible = eligible[-batch_size:]
        selected = eligible[:batch_size]
        if not selected:
            return validity, after, [], False
        identifiers = ",".join(map(str, selected))
        state, sizes = mailbox.uid("FETCH", identifiers, "(UID RFC822.SIZE)")
        if state != "OK":
            raise MailError("Lettura dimensioni IMAP non riuscita")
        safe = set()
        for payload in sizes:
            if isinstance(payload, bytes):
                uid = re.search(rb"\bUID (\d+)\b", payload)
                size = re.search(rb"\bRFC822\.SIZE (\d+)\b", payload)
                if uid and size and int(size[1]) <= 512_000:
                    safe.add(int(uid[1]))
        messages = []
        if safe:
            state, bodies = mailbox.uid("FETCH", ",".join(map(str, sorted(safe))), "(UID BODY.PEEK[])")
            if state != "OK":
                raise MailError("Lettura messaggi IMAP non riuscita")
            for item in bodies:
                if isinstance(item, tuple):
                    uid = re.search(rb"\bUID (\d+)\b", item[0])
                    if uid and int(uid[1]) in safe:
                        messages.append((int(uid[1]), item[1]))
        return validity, selected[-1], messages, len(eligible) > len(selected)
    except (OSError, imaplib.IMAP4.abort) as error:
        raise MailError("Connessione IMAP interrotta") from error
    finally:
        with suppress(OSError, imaplib.IMAP4.error):
            mailbox.logout()
