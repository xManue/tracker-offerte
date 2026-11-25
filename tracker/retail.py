import hashlib
import hmac
import time
from datetime import timedelta, timezone

from .connectors import FetchError, Page, checked
from .db import now

AMAZON_MARKET = "www.amazon.it"
AMAZON_TOKEN = "https://api.amazon.co.uk/auth/o2/token"
AMAZON_SEARCH = "https://creatorsapi.amazon/catalog/v1/searchItems"
ALIEXPRESS_TOP = "https://eco.taobao.com/router/rest"
ALIEXPRESS_QUERY = "aliexpress.affiliate.product.query"


class AmazonCreators:
    supports_refresh = False

    def __init__(self, client, key, secret, tag):
        self.http, self.key, self.secret, self.tag = client, key, secret, tag
        self.token = None
        self.expires = 0

    def headers(self):
        if self.token is None or time.monotonic() >= self.expires:
            body = checked(self.http.post(AMAZON_TOKEN, json={
                "grant_type": "client_credentials", "client_id": self.key, "client_secret": self.secret,
                "scope": "creatorsapi::default",
            }))
            if not isinstance(body.get("access_token"), str) or not body["access_token"]:
                raise FetchError("Token Creators assente", retry=False)
            self.token = body["access_token"]
            self.expires = time.monotonic() + max(0, int(body.get("expires_in", 300)) - 20)
        return {"Authorization": f"Bearer {self.token}", "x-marketplace": AMAZON_MARKET}

    def discover(self, query, cursor):
        page = int(cursor or 1)
        page_size = 10
        body = checked(self.http.post(AMAZON_SEARCH, headers=self.headers(), json={
            "keywords": query, "itemPage": page, "itemCount": page_size,
            "partnerTag": self.tag, "marketplace": AMAZON_MARKET,
            "resources": ["itemInfo.title", "images.primary.medium", "offersV2.listings.price",
                          "offersV2.listings.condition", "offersV2.listings.availability",
                          "offersV2.listings.dealDetails", "offersV2.listings.isBuyBoxWinner"],
        }))
        result = body.get("searchResult")
        if not isinstance(result, dict):
            raise FetchError("Risposta Creators senza risultati consultabili")
        items = result.get("items") or []
        if not isinstance(items, list):
            raise FetchError("Elenco prodotti Creators non valido")
        rows = [self.normalize(item) for item in items if isinstance(item, dict)]
        count = int(result.get("totalResultCount") or 0)
        next_page = str(page + 1) if page < 10 and count > page * page_size else None
        return Page(rows, next_page)

    @staticmethod
    def normalize(item):
        listings = (item.get("offersV2") or {}).get("listings") or []
        chosen = next((offer for offer in listings if offer.get("isBuyBoxWinner")),
                      listings[0] if len(listings) == 1 else {})
        price = (chosen.get("price") or {}).get("money") or {}
        if (chosen.get("dealDetails") or {}).get("accessType") not in (None, "ALL"):
            price = {}
        title = ((item.get("itemInfo") or {}).get("title") or {}).get("displayValue")
        image = (((item.get("images") or {}).get("primary") or {}).get("medium") or {}).get("url", "")
        status = ((chosen.get("availability") or {}).get("type") or "").upper()
        condition = ((chosen.get("condition") or {}).get("value") or "").casefold()
        return {"external_id": item.get("asin"), "title": title, "url": item.get("detailPageURL", ""),
                "image_url": image, "market": "IT", "price": price.get("amount"),
                "currency": price.get("currency") or "EUR", "condition": condition,
                "status": "available" if status in {"NOW", "IN_STOCK"} else "unknown",
                "checked_by": "creators"}


class AliExpressAffiliate:
    supports_refresh = False

    def __init__(self, client, key, secret, tracking):
        self.http, self.key, self.secret, self.tracking = client, key, secret, tracking

    def discover(self, query, cursor):
        page = int(cursor or 1)
        params = {
            "method": ALIEXPRESS_QUERY, "app_key": self.key, "sign_method": "hmac",
            "timestamp": now().astimezone(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S"),
            "format": "json", "v": "2.0", "keywords": query, "tracking_id": self.tracking,
            "ship_to_country": "IT", "target_language": "IT", "target_currency": "EUR",
            "page_no": str(page), "page_size": "20",
        }
        payload = "".join(key + params[key] for key in sorted(params)).encode("utf-8")
        params["sign"] = hmac.new(self.secret.encode("utf-8"), payload, hashlib.md5).hexdigest().upper()
        body = checked(self.http.post(ALIEXPRESS_TOP, data=params))
        fault = body.get("error_response")
        if isinstance(fault, dict):
            code = str(fault.get("code", ""))
            if code in {"7", "8", "9", "11", "12", "13", "21", "25", "27"}:
                raise FetchError("Accesso o autorizzazione Affiliate da verificare", retry=False)
            raise FetchError("Errore restituito da AliExpress Affiliate")
        response = (body.get("aliexpress_affiliate_product_query_response") or {}).get("resp_result") or {}
        if str(response.get("resp_code")) != "200":
            raise FetchError("Risposta Affiliate non valida o accesso da verificare")
        result = response.get("result") or {}
        items = (result.get("products") or {}).get("product") or []
        if not isinstance(items, list):
            raise FetchError("Elenco prodotti Affiliate non valido")
        count = int(result.get("total_page_no") or page)
        return Page([self.normalize(item) for item in items if isinstance(item, dict)],
                    str(page + 1) if page < count else None)

    @staticmethod
    def normalize(item):
        currency = item.get("target_sale_price_currency") or item.get("sale_price_currency") or "EUR"
        price = item.get("target_sale_price")
        if price is None and item.get("sale_price_currency") == currency:
            price = item.get("sale_price")
        identifier = item.get("product_id")
        return {"external_id": str(identifier) if identifier is not None else "", "title": item.get("product_title"),
                "url": item.get("product_detail_url", ""), "image_url": item.get("product_main_image_url", ""),
                "price": price, "currency": currency, "market": "IT", "status": "unknown",
                "condition": "unknown", "checked_by": "affiliate"}
