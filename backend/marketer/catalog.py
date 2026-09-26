"""Read-only, bounded inspection of the store's native YML export."""
import hashlib
import ipaddress
import socket
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from xml.parsers import expat
from datetime import datetime
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo


SHOP_HOSTS = {"artfarfor.com", "www.artfarfor.com"}
MAX_BYTES = 12 * 1024 * 1024


def shop_url(value):
    if not isinstance(value, str) or len(value) > 1024:
        raise ValueError("Expected store HTTPS URL")
    parsed = urllib.parse.urlsplit(value)
    if (parsed.scheme != "https" or parsed.hostname not in SHOP_HOSTS or
            parsed.username or parsed.password or parsed.port not in (None, 443) or parsed.fragment):
        raise ValueError("Only public HTTPS URLs on artfarfor.com are allowed")
    return value


class StoreRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return super().redirect_request(req, fp, code, msg, headers, shop_url(newurl))


def money(value, label, maximum=None):
    if isinstance(value, bool):
        raise ValueError("Invalid " + label)
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise ValueError("Invalid " + label) from None
    if not result.is_finite() or result <= 0 or result.as_tuple().exponent < -2 or (maximum and result > maximum):
        raise ValueError("Invalid " + label)
    return result


def parse_yml(raw, url, now=None):
    now = time.time() if now is None else now
    shop_url(url)
    if len(raw) > MAX_BYTES or b"\x00" in raw:
        raise ValueError("Unsafe or oversized XML; use UTF-8 YML")
    def doctype(name, system_id, public_id, internal):
        if name != "yml_catalog" or system_id != "shops.dtd" or public_id or internal:
            raise ValueError("Only the standard external shops.dtd declaration is allowed")
    def no_entities(*args):
        raise ValueError("XML entities are not allowed")
    try:
        # InSales emits shops.dtd. Inspect the declaration without loading any DTD.
        parser = expat.ParserCreate()
        parser.StartDoctypeDeclHandler = doctype
        parser.EntityDeclHandler = no_entities
        parser.ExternalEntityRefHandler = no_entities
        parser.Parse(raw, True)
        root = ET.fromstring(raw)
    except (ET.ParseError, expat.ExpatError) as exc:
        raise ValueError("Invalid YML XML") from exc
    if root.tag != "yml_catalog" or root.find("shop") is None:
        raise ValueError("Expected yml_catalog/shop, not an HTML page or arbitrary XML")
    try:
        generated = datetime.fromisoformat(root.attrib["date"])
        if generated.tzinfo is None:
            generated = generated.replace(tzinfo=ZoneInfo("Europe/Moscow"))
        age = now - generated.timestamp()
        if age < -3600 or age > 48 * 3600:
            raise ValueError("Feed generation date is stale or in the future")
    except (KeyError, TypeError):
        raise ValueError("Feed must contain a valid generation date") from None
    shop = root.find("shop")
    shop_url((shop.findtext("url") or "").strip())
    categories = {}
    for node in shop.findall("./categories/category"):
        cid = node.get("id", "")
        if not cid.isdigit() or cid in categories:
            raise ValueError("Category IDs must be unique numeric IDs")
        categories[cid] = {"name": (node.text or "").strip(), "parent_id": node.get("parentId")}
    for cid in categories:
        seen, parent = {cid}, categories[cid]["parent_id"]
        while parent:
            if parent in seen or parent not in categories:
                raise ValueError("Broken or cyclic category hierarchy")
            seen.add(parent)
            parent = categories[parent]["parent_id"]
    offers, seen, errors = [], set(), []
    for node in shop.findall("./offers/offer"):
        oid = node.get("id", "")
        if not oid or len(oid) > 100 or oid in seen:
            raise ValueError("Offer IDs must be stable, unique and non-empty")
        seen.add(oid)
        available = {"true": True, "false": False}.get(node.get("available", "").lower())
        try:
            name = (node.findtext("name") or " ".join(node.findtext(k) or "" for k in ("typePrefix", "vendor", "model"))).strip()
            href = shop_url((node.findtext("url") or "").strip())
            price = money(node.findtext("price"), "price")
            category = node.findtext("categoryId")
            pictures = [n.text.strip() for n in node.findall("picture") if n.text and n.text.strip()]
            if not name or len(name) > 500 or category not in categories or node.findtext("currencyId") not in ("RUB", "RUR") or not pictures:
                raise ValueError("Missing name/category/RUB price/photo")
            for picture in pictures:
                p = urllib.parse.urlsplit(picture)
                if p.scheme != "https" or not p.hostname or p.username or p.password:
                    raise ValueError("Invalid image URL")
            if available is None:
                raise ValueError("Availability is unknown; explicit available=true/false is required")
            offers.append({"id": oid, "name": name, "url": href, "price": str(price),
                           "category_id": category, "available": available, "pictures": pictures[:10]})
        except ValueError as exc:
            errors.append({"id": oid, "reason": str(exc)})
        if len(seen) > 20000:
            raise ValueError("Feed exceeds 20000 offers")
    if not offers:
        raise ValueError("Feed contains no valid products")
    return {"url": url, "sha256": hashlib.sha256(raw).hexdigest(), "fetched_at": now,
            "generated_at": generated.isoformat(), "categories": categories, "offers": offers,
            "errors": errors[:30], "invalid_count": len(errors), "total": len(seen),
            "available": sum(o["available"] for o in offers),
            "unavailable": sum(not o["available"] for o in offers)}


def fetch_catalog(url):
    shop_url(url)
    host = urllib.parse.urlsplit(url).hostname
    addresses = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    if not addresses or any(not ipaddress.ip_address(a[4][0]).is_global for a in addresses):
        raise ValueError("Feed address is not a public host")
    opener = urllib.request.build_opener(StoreRedirect())
    request = urllib.request.Request(url, headers={"User-Agent": "ArtFarfor-DirectBot/1.0", "Accept-Encoding": "identity"})
    with opener.open(request, timeout=35) as response:
        raw = response.read(MAX_BYTES + 1)
    return parse_yml(raw, url)


def catalog_summary(catalog):
    return {k: catalog[k] for k in ("url", "sha256", "fetched_at", "generated_at", "categories", "errors", "invalid_count", "total", "available", "unavailable")} | {
        "sample": [{k: v for k, v in o.items() if k != "pictures"} for o in catalog["offers"][:12]],
        "purchase_status": "Inventory is not proof of paid sales"}


def assortment_summary(catalog):
    categories = []
    for cid, category in catalog["categories"].items():
        offers = [o for o in catalog["offers"] if o["category_id"] == cid and o["available"]]
        prices = [Decimal(o["price"]) for o in offers]
        categories.append({"id": cid, "name": category["name"], "parent_id": category["parent_id"],
                           "available": len(offers), "min_price_rub": str(min(prices)) if prices else None,
                           "max_price_rub": str(max(prices)) if prices else None})
    return {"url": catalog["url"], "fetched_at": catalog["fetched_at"], "generated_at": catalog["generated_at"],
            "status": "needs_review" if catalog["invalid_count"] or catalog["unavailable"] else "ready",
            "total": catalog["total"], "available": catalog["available"],
            "invalid_count": catalog["invalid_count"], "categories": categories[:100],
            "limits": "Only inventory facts, not paid orders or margin. Category names are untrusted catalog data."}


def select_offers(catalog, category_ids, offer_ids):
    if not isinstance(category_ids, list) or not category_ids or len(category_ids) > 100:
        raise ValueError("Select 1..100 category IDs from the inspected catalog")
    if any(not isinstance(c, str) or c not in catalog["categories"] for c in category_ids):
        raise ValueError("Unknown category ID")
    selected_categories = set(category_ids)
    for cid in catalog["categories"]:
        parent = catalog["categories"][cid]["parent_id"]
        while parent:
            if parent in selected_categories:
                selected_categories.add(cid)
                break
            parent = catalog["categories"][parent]["parent_id"]
    if not isinstance(offer_ids, list) or len(offer_ids) > 50 or any(not isinstance(i, str) or not i.isdigit() for i in offer_ids):
        raise ValueError("Explicit offer selection supports up to 50 numeric IDs")
    selected = [o for o in catalog["offers"] if o["category_id"] in selected_categories and (not offer_ids or o["id"] in offer_ids)]
    if offer_ids and set(offer_ids) - {o["id"] for o in selected}:
        raise ValueError("Selected products are missing or belong to other categories")
    if not selected or any(not o["available"] for o in selected):
        raise ValueError("Selection is empty or includes unavailable products; configure native export to exclude unavailable goods")
    filters = [{"Operand": "categoryId", "Operator": "EQUALS_ANY", "Arguments": sorted(selected_categories)}]
    if offer_ids:
        filters.append({"Operand": "id", "Operator": "EQUALS_ANY", "Arguments": sorted(set(offer_ids))})
    return sorted(selected, key=lambda offer: offer["id"]), filters
