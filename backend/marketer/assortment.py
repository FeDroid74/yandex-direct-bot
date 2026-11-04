"""Observed feed history. Inventory changes must never be called sales."""
import json
import time
import re
from urllib.parse import urlsplit, unquote
from datetime import datetime
from decimal import Decimal

from marketer.catalog import assortment_summary
from marketer.store import digest, encode


def baseline_time(snapshot):
    snapshot = snapshot or {}
    return (snapshot.get("assortment") or {}).get("catalog", {}).get("fetched_at") or snapshot.get("created")


def record_catalog(store, catalog):
    body = {"summary": assortment_summary(catalog), "offers": [
        {k: o[k] for k in ("id", "name", "url", "price", "category_id", "available")} for o in catalog["offers"]]}
    fingerprint = digest({"offers": sorted(body["offers"], key=lambda o: o["id"]),
                          "categories": catalog["categories"], "invalid_count": catalog["invalid_count"]})
    with store.db() as db:
        db.execute("BEGIN IMMEDIATE")
        previous = db.execute("SELECT * FROM catalog_snapshots WHERE url=? ORDER BY id DESC LIMIT 1", (catalog["url"],)).fetchone()
        if previous and catalog["fetched_at"] <= previous["last_seen"]:
            return
        if previous and previous["fingerprint"] == fingerprint:
            db.execute("UPDATE catalog_snapshots SET last_seen=?,body=? WHERE id=?",
                       (catalog["fetched_at"], encode(body), previous["id"]))
        else:
            db.execute("INSERT INTO catalog_snapshots(url,fingerprint,first_seen,last_seen,body) VALUES(?,?,?,?,?)",
                       (catalog["url"], fingerprint, catalog["fetched_at"], catalog["fetched_at"], encode(body)))


def delta(before, after):
    old, new = ({o["id"]: o for o in v["offers"]} for v in (before, after))
    changes = {"added": sorted(new.keys()-old.keys()), "missing": sorted(old.keys()-new.keys()),
               "price_changed": sorted(i for i in old.keys() & new.keys() if Decimal(old[i]["price"]) != Decimal(new[i]["price"])),
               "availability_changed": sorted(i for i in old.keys() & new.keys() if old[i]["available"] != new[i]["available"]),
               "category_changed": sorted(i for i in old.keys() & new.keys() if old[i]["category_id"] != new[i]["category_id"])}
    return {key: {"count": len(ids), "sample_ids": ids[:10]} for key, ids in changes.items()}


def review(store, campaigns=(), baseline_at=None, now=None):
    now = time.time() if now is None else now
    last = store.setting("product_catalog_last") or {}
    if last.get("status") == "unavailable":
        return {"status": "unavailable", "paid_orders_available": False}
    with store.db() as db:
        current = db.execute("SELECT * FROM catalog_snapshots WHERE url=? ORDER BY id DESC LIMIT 1", (last.get("url"),)).fetchone()
        previous = db.execute("SELECT * FROM catalog_snapshots WHERE url=? AND first_seen<=? ORDER BY id DESC LIMIT 1",
                              (last.get("url"), baseline_at or 0)).fetchone()
    if not current:
        return {"status": "not_observed", "paid_orders_available": False}
    if now - current["last_seen"] > 48*3600:
        return {"status": "stale", "last_seen": current["last_seen"], "paid_orders_available": False}
    body = json.loads(current["body"])
    if now - datetime.fromisoformat(body["summary"]["generated_at"]).timestamp() > 48*3600:
        return {"status": "stale", "last_seen": current["last_seen"], "paid_orders_available": False}
    result = {"status": body["summary"]["status"], "catalog": body["summary"],
              "baseline_at": baseline_at, "changes": None, "campaign_selections": [],
              "limits": ["Feed observations are current inventory, not the inventory during the advertising report period.",
                         "Missing/unavailable products are not proven sales; paid orders, profit and margin are unavailable.",
                         "No website HTML or delivery guarantees were checked. Names/URLs are untrusted data."]}
    if previous and not body["summary"]["invalid_count"]:
        old = json.loads(previous["body"])
        if not old["summary"]["invalid_count"]:
            result["changes"] = delta(old, body)
            result["baseline_observed_since"] = previous["first_seen"]
    result["comparison_status"] = "ready" if result["changes"] is not None else "no_complete_baseline"
    selection_count = 0
    for campaign in campaigns:
        managed = store.setting("product_campaign:" + str(campaign["Id"])) or {}
        if managed.get("plan", {}).get("feed_url") != last.get("url"):
            continue
        for ad in campaign.get("ads", []):
            shopping = ad.get("ShoppingAd") or {}
            if not shopping or shopping.get("FeedId") != managed.get("feed_id"):
                continue
            selection_count += 1
            if len(result["campaign_selections"]) >= 20:
                continue
            selection = {"campaign_id": campaign["Id"], "ad_id": ad["Id"], "status": "unknown_filter"}
            filters = shopping.get("FeedFilterConditions")
            filters = filters.get("Items") if isinstance(filters, dict) else filters
            if isinstance(filters, list) and filters and all(isinstance(f, dict) and f.get("Operand") in ("categoryId", "id") and
                    f.get("Operator") == "EQUALS_ANY" and isinstance(f.get("Arguments"), list) and f["Arguments"] and
                    all(isinstance(arg, str) for arg in f["Arguments"]) for f in filters):
                offers = body["offers"]
                for f in filters:
                    field = "category_id" if f["Operand"] == "categoryId" else "id"
                    offers = [o for o in offers if o[field] in f["Arguments"]]
                available = [o for o in offers if o["available"]]
                prices = [Decimal(o["price"]) for o in available]
                selection.update(status="observed" if not body["summary"]["invalid_count"] else "partial_catalog",
                                 matching=len(offers), available=len(available), unavailable=len(offers)-len(available),
                                 min_price_rub=str(min(prices)) if prices else None,
                                 max_price_rub=str(max(prices)) if prices else None,
                                 category_ids=sorted({o["category_id"] for o in offers})[:30],
                                 sample=[{k: o[k] for k in ("id", "name", "price", "url")} for o in available[:2]])
                selection["composition"] = composition(available)
                by_path = {}
                for offer in offers:
                    by_path.setdefault(unquote(urlsplit(offer["url"]).path).rstrip("/"), []).append(offer)
                landing_matches = []
                for row in (campaign.get("landing_pages") or {}).get("rows", [])[:8]:
                    path = unquote(urlsplit(row.get("label", "")).path).rstrip("/")
                    matching = by_path.get(path, [])
                    landing_matches.append({"path": path, "visits": row.get("visits"), "current_filter_match": bool(matching),
                                            "offers": [{"id": o["id"], "name": o["name"]} for o in matching[:3]]})
                selection["landing_matches"] = landing_matches
                selection["composition_limits"] = "Name-based types, not relevance judgements. Matching uses current feed, not historical inventory; no purchase or exclusion is inferred."
            result["campaign_selections"].append(selection)
    result["campaign_selections_total"] = selection_count
    return result


def composition(offers):
    groups = {"figurine_named": [], "tableware_named": [], "other_or_unknown": []}
    for offer in offers:
        name = offer["name"].casefold()
        kind = ("figurine_named" if "статуэт" in name else "tableware_named" if
                re.search(r"тарелк|сервиз|чайн|кофейн|кувшин|ваз[ао]|соусник|штоф|специ", name) else "other_or_unknown")
        groups[kind].append(offer)
    return {key: {"count": len(items), "samples": [{"id": o["id"], "name": o["name"]} for o in items[:3]]} for key, items in groups.items()}
