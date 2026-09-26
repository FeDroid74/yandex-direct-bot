"""Product campaign drafts and owner-approved, journaled Direct operations."""
import copy
import json
import time
import uuid
from datetime import datetime
from zoneinfo import ZoneInfo

from marketer.catalog import assortment_summary, catalog_summary, fetch_catalog, money, select_offers, shop_url
from marketer.data import checked
from marketer.store import Conflict, digest, encode


PURCHASE_GOAL = 352606262
COUNTER_ID = 99041859
KINDS = {"product_feed_register", "product_create", "product_complete_create", "product_edit", "product_ad_edit", "product_moderate", "product_launch", "product_pause_old"}
SHOPPING_FIELDS = ["FeedId", "DefaultTexts", "FeedFilterConditions", "TitleSources", "TextSources"]


def positive(value, name):
    if type(value) is not int or value <= 0:
        raise ValueError("Invalid " + name)
    return value


def string(value, name, maximum):
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise ValueError("Invalid " + name)
    return value.strip()


def exact(value, required, optional=()):
    if not isinstance(value, dict) or set(value) - set(required) - set(optional) or set(required) - set(value):
        raise ValueError("Unexpected or missing fields; required: " + ", ".join(sorted(required)))


def native_feed_matches(feed, url, allow_pending=False):
    if feed.get("SourceType") != "URL" or (feed.get("UrlFeed") or {}).get("Url") != url:
        return False
    # Production can return OTHER for a RETAIL add; accept only its known YML schema.
    compatible_schema = feed.get("FilterSchema") == "PerformanceDefault"
    pending_schema = allow_pending and feed.get("Status") in ("NEW", "UPDATING") and not feed.get("FilterSchema")
    return feed.get("BusinessType") == "RETAIL" or (
        feed.get("BusinessType") == "OTHER" and (compatible_schema or pending_schema)
    )


def feed_processed(feed):
    count = feed.get("NumberOfItems")
    return feed.get("Status") == "DONE" and type(count) is int and count > 0


def inactive_draft(campaign):
    return campaign.get("Status") == "DRAFT" and campaign.get("State") in ("OFF", "SUSPENDED")


def purchase_strategy(campaign):
    unified = campaign.get("UnifiedCampaign", {})
    if campaign.get("Type") != "UNIFIED_CAMPAIGN" or unified.get("PackageBiddingStrategy"):
        raise Conflict("Only a standalone Unified Campaign with purchase billing is supported")
    if COUNTER_ID not in (unified.get("CounterIds") or {}).get("Items", []):
        raise Conflict("The required Metrika counter is not linked")
    strategy = unified.get("BiddingStrategy", {})
    active = []
    for key in ("Search", "Network"):
        side = strategy.get(key, {})
        kind = side.get("BiddingStrategyType")
        if kind == "SERVING_OFF":
            continue
        if kind == "NETWORK_DEFAULT" and key == "Network":
            if strategy.get("Search", {}).get("BiddingStrategyType") != "PAY_FOR_CONVERSION":
                raise Conflict("Network must inherit purchase conversion billing")
            continue
        values = side.get("PayForConversion", {})
        if kind != "PAY_FOR_CONVERSION" or values.get("GoalId") != PURCHASE_GOAL:
            raise Conflict("Only PAY_FOR_CONVERSION for purchase goal 352606262 is permitted")
        if not values.get("Cpa") or not values.get("WeeklySpendLimit") or values["WeeklySpendLimit"] < 20 * values["Cpa"]:
            raise Conflict("Invalid purchase CPA/weekly budget")
        active.append(key)
    if not active:
        raise Conflict("No active purchase conversion strategy")
    return active


def replacement_candidates(snapshot, now=None):
    now = time.time() if now is None else now
    if not snapshot or now - snapshot.get("created", 0) > 7 * 86400:
        return {"status": "needs_fresh_statistics", "items": []}
    items = []
    for c in snapshot.get("campaigns", []):
        current = c.get("current", {})
        primary = current.get("GoalTotals", {}).get(str(PURCHASE_GOAL), {})
        count = primary.get("confirmed_sum") if primary.get("complete") else None
        strategy = c.get("UnifiedCampaign", {}).get("BiddingStrategy", {})
        targets = [v["PayForConversion"]["Cpa"] / 1e6 for v in strategy.values()
                   if v.get("BiddingStrategyType") == "PAY_FOR_CONVERSION" and v.get("PayForConversion", {}).get("GoalId") == PURCHASE_GOAL]
        clicks, cost = current.get("Clicks", 0), current.get("Cost", 0)
        status, reason = "insufficient_data", "Purchase totals are unknown or the mature sample is too small"
        if count is not None and count > 0:
            status, reason = "preserve", "Confirmed purchase-goal conversions; do not replace solely for low volume"
            if count >= 10 and targets and cost / count > max(targets) * 1.5:
                status, reason = "review_candidate", "At least 10 purchase-goal conversions, observed CPA > 1.5x configured purchase CPA"
        elif (count == 0 and clicks >= 100 and c.get("diagnostics", {}).get("counter_linked") and
              c.get("diagnostics", {}).get("goals", {}).get(str(PURCHASE_GOAL), {}).get("present") is True):
            status, reason = "review_candidate", "At least 100 mature clicks and a complete numeric zero for the purchase goal"
        if any(a.get("Type") == "SHOPPING_AD" for a in c.get("ads", [])):
            status, reason = "already_product", "Already contains product ads; review settings instead of replacing automatically"
        try:
            purchase_strategy(c)
            compliant = True
        except Conflict:
            compliant = False
        items.append({"campaign_id": c["Id"], "name": c["Name"], "status": status, "reason": reason,
                      "clicks": clicks, "cost_rub": cost, "purchase_goal_conversions": count,
                      "purchase_billing_compliant": compliant})
    return {"status": "ready", "date_from": snapshot.get("current_from"), "date_to": snapshot.get("date_to"),
            "items": items, "limits": "Goal achievements are not paid orders. Candidates require human review; nothing is stopped automatically."}


class Products:
    def __init__(self, source, store, fetcher=fetch_catalog):
        self.source, self.store, self.fetcher = source, store, fetcher
        with store.db() as db:
            db.execute("CREATE TABLE IF NOT EXISTS product_drafts (id TEXT PRIMARY KEY, revision INTEGER NOT NULL, body TEXT NOT NULL, updated REAL NOT NULL)")

    def catalog(self, url):
        result = self.fetcher(shop_url(url))
        self.store.set_setting("product_catalog:" + digest(url), result)
        self.store.set_setting("product_catalog_last", assortment_summary(result))
        return catalog_summary(result)

    def validate(self, raw, catalog):
        exact(raw, {"name", "feed_url", "placement_type", "region_ids", "target_cpa_rub", "weekly_budget_rub", "groups"},
              {"replacement_for_campaign_id"})
        if catalog["invalid_count"] or catalog["unavailable"]:
            raise ValueError("Native export must contain only valid, explicitly available products; exclude unavailable goods in InSales")
        if raw["feed_url"] != catalog["url"]:
            raise ValueError("Catalog URL mismatch")
        placement = raw["placement_type"]
        if placement not in ("search_only", "network_only", "both"):
            raise ValueError("Choose search_only, network_only or both explicitly")
        regions = raw["region_ids"]
        if not isinstance(regions, list) or not regions or len(regions) > 100 or any(type(i) is not int for i in regions) or not any(i >= 0 for i in regions) or (0 in regions and len(regions) != 1):
            raise ValueError("Invalid explicit region IDs")
        cpa, budget = money(raw["target_cpa_rub"], "target CPA", 30000), money(raw["weekly_budget_rub"], "weekly budget", 300000)
        if budget < cpa * 20:
            raise ValueError("Weekly budget must be at least 20 purchase CPAs; do not increase it silently")
        groups = raw["groups"]
        if not isinstance(groups, list) or not 1 <= len(groups) <= 10:
            raise ValueError("Create 1..10 thematic groups")
        normalized, seen = [], set()
        for group in groups:
            exact(group, {"name", "category_ids", "default_text"}, {"offer_ids"})
            selected, filters = select_offers(catalog, group["category_ids"], group.get("offer_ids", []))
            ids = {o["id"] for o in selected}
            if ids & seen:
                raise ValueError("Product selections overlap between groups")
            seen |= ids
            text = string(group["default_text"], "default_text", 81)
            if any(len(word) > 23 for word in text.split()):
                raise ValueError("Default text contains a word longer than 23 characters")
            normalized.append({"name": string(group["name"], "group name", 120),
                               "category_ids": sorted(set(group["category_ids"])), "offer_ids": sorted(set(group.get("offer_ids", []))),
                               "default_text": text, "filters": filters, "product_count": len(selected),
                               "selection_digest": digest(selected)})
        definition = {"name": string(raw["name"], "campaign name", 120), "feed_url": shop_url(raw["feed_url"]),
                      "placement_type": placement, "region_ids": regions, "target_cpa_rub": str(cpa),
                      "weekly_budget_rub": str(budget), "groups": normalized,
                      "goal_id": PURCHASE_GOAL, "counter_id": COUNTER_ID, "billing": "PAY_FOR_CONVERSION"}
        if raw.get("replacement_for_campaign_id") is not None:
            definition["replacement_for_campaign_id"] = positive(raw["replacement_for_campaign_id"], "replacement campaign")
        return definition

    @staticmethod
    def editable(plan):
        result = {k: copy.deepcopy(plan[k]) for k in ("name", "feed_url", "placement_type", "region_ids", "target_cpa_rub", "weekly_budget_rub", "groups")}
        if plan.get("replacement_for_campaign_id"):
            result["replacement_for_campaign_id"] = plan["replacement_for_campaign_id"]
        result["groups"] = [{k: v for k, v in g.items() if k in ("name", "category_ids", "offer_ids", "default_text")} for g in result["groups"]]
        return result

    def save_draft(self, raw, draft_id=None, revision=None):
        catalog = self.fetcher(shop_url(raw.get("feed_url")))
        plan = self.validate(raw, catalog)
        if plan.get("replacement_for_campaign_id"):
            original = self.source.campaign(plan["replacement_for_campaign_id"])
            if original.get("State") == "ARCHIVED":
                raise ValueError("Cannot replace an archived campaign")
        with self.store.db() as db:
            db.execute("BEGIN IMMEDIATE")
            if draft_id:
                row = db.execute("SELECT * FROM product_drafts WHERE id=?", (draft_id,)).fetchone()
                if not row or row["revision"] != revision:
                    raise Conflict("Draft has changed; read its current revision")
                revision += 1
                db.execute("UPDATE product_drafts SET revision=?,body=?,updated=? WHERE id=?", (revision, encode(plan), time.time(), draft_id))
            else:
                draft_id, revision = uuid.uuid4().hex[:12], 1
                db.execute("INSERT INTO product_drafts VALUES(?,?,?,?)", (draft_id, revision, encode(plan), time.time()))
        return self.get_draft(draft_id)

    def get_draft(self, draft_id):
        with self.store.db() as db:
            row = db.execute("SELECT * FROM product_drafts WHERE id=?", (draft_id,)).fetchone()
        if not row:
            raise Conflict("Product draft not found")
        return {"id": row["id"], "revision": row["revision"], "updated": row["updated"], "plan": json.loads(row["body"])}

    def feeds(self):
        return self.source.entities("feeds", "Feeds", {"FieldNames": ["Id", "Name", "Status", "SourceType", "BusinessType", "NumberOfItems", "FilterSchema"], "UrlFeedFieldNames": ["Url"]})

    def ready_feed(self, url):
        ready = [f for f in self.feeds() if native_feed_matches(f, url) and feed_processed(f)]
        if not ready:
            raise Conflict("Сначала зарегистрируйте этот URL отдельной карточкой фида и дождитесь успешной обработки Директом.")
        return sorted(ready, key=lambda f: f["Id"])[0]

    def reconcile_feed_registration(self, proposal_id):
        """Reconcile a confirmed feed ID with reads only; never replay a write."""
        row = self.store.get(proposal_id)
        action = row["body"]["action"]
        if action.get("kind") != "product_feed_register" or row["state"] != "uncertain":
            raise Conflict("Only an uncertain feed registration can be reconciled")
        operation = self.operation(proposal_id)
        steps = operation.get("steps", [])
        if operation.get("target") != "production" or len(steps) != 1:
            raise Conflict("A single confirmed production feed write is required")
        step = steps[0]
        ids = step.get("ids", [])
        if (step.get("service"), step.get("method"), step.get("status")) != ("feeds", "add", "confirmed") or len(ids) != 1:
            raise Conflict("Feed creation result is not confirmed; do not retry")
        feed_id = positive(ids[0], "feed ID")
        claim_key = "product_feed_claim:" + digest(action["feed_url"])
        claim = self.store.setting(claim_key, {})
        if claim.get("operation") != proposal_id:
            raise Conflict("Feed operation claim does not match")
        matches = [f for f in self.feeds() if f.get("Id") == feed_id]
        if len(matches) != 1 or not native_feed_matches(matches[0], action["feed_url"]) or not feed_processed(matches[0]):
            raise Conflict("Confirmed feed ID is not a matching, successfully processed URL feed")
        result = {"feed_id": feed_id, "processing_status": matches[0]["Status"],
                  "campaign_created": False, "reconciled": True, "observed_feed": matches[0]}
        with self.store.db() as db:
            db.execute("BEGIN IMMEDIATE")
            current = self.store.unpack(db.execute("SELECT * FROM proposals WHERE id=?", (proposal_id,)).fetchone())
            approved = db.execute("SELECT 1 FROM events WHERE proposal_id=? AND revision=? AND event='approve'",
                                  (proposal_id, row["revision"])).fetchone()
            if current["state"] != "uncertain" or current["revision"] != row["revision"] or current["body"] != row["body"] or not approved:
                raise Conflict("An unchanged, previously owner-approved operation is required")
            now = time.time()
            db.execute("UPDATE proposals SET state='applied',result=?,updated=?,applied_at=? WHERE id=?",
                       (encode(result), now, now, proposal_id))
            self.store.event(db, current, "feed_registration_reconciled", "reconciler", result)
        return self.store.get(proposal_id)

    def operation(self, proposal_id):
        return self.store.setting("product_operation:" + proposal_id, {"status": "not_attempted"})

    def claim(self, key, operation):
        with self.store.db() as db:
            db.execute("BEGIN IMMEDIATE")
            if not db.execute("INSERT OR IGNORE INTO settings VALUES(?,?)", (key, encode({"operation": operation, "started": time.time()}))).rowcount:
                raise Conflict("Создание уже запускалось. Требуется сверка журнала, повторное создание запрещено.")

    def ads(self, cid):
        return sorted(self.source.entities("ads", "Ads", {"SelectionCriteria": {"CampaignIds": [cid]},
                                    "FieldNames": ["Id", "CampaignId", "AdGroupId", "Type", "State", "Status"],
                                    "ShoppingAdFieldNames": SHOPPING_FIELDS}, allow_empty_result=True), key=lambda ad: ad["Id"])

    def empty_creation(self, cid, source_proposal_id):
        original = self.store.get(source_proposal_id)
        if original["state"] not in ("uncertain", "partial") or original["body"]["action"].get("kind") != "product_create":
            raise Conflict("Continuation requires an inspected partial product creation")
        operation = self.operation(source_proposal_id)
        steps = operation.get("steps", [])
        if operation.get("target") != "production" or len(steps) != 2:
            raise Conflict("Only the known empty-draft suspension failure can be continued")
        added, suspended = steps
        errors = suspended.get("result", {}).get("SuspendResults", [])
        if ((added.get("service"), added.get("method"), added.get("status"), added.get("ids")) != ("campaigns", "add", "confirmed", [cid]) or
                (suspended.get("service"), suspended.get("method"), suspended.get("status")) != ("campaigns", "suspend", "rejected_or_partial") or
                len(errors) != 1 or errors[0].get("Id") or [e.get("Code") for e in errors[0].get("Errors", [])] != [8300]):
            raise Conflict("The journal does not prove an empty draft with a rejected suspension")
        draft_id = original["body"]["action"]["draft_id"]
        draft = self.get_draft(draft_id)
        if draft["revision"] != original["body"]["action"]["draft_revision"] or draft["plan"] != original["body"]["product_plan"]:
            raise Conflict("Original draft changed; continuation needs a new reviewed plan")
        claim = self.store.setting("product_draft_claim:" + draft_id, {})
        deployed = self.store.setting("product_deployed:" + draft_id, {})
        if claim.get("operation") != source_proposal_id or deployed != {"campaign_id": cid, "operation": source_proposal_id}:
            raise Conflict("Campaign does not match the recorded creation and draft claim")
        with self.store.db() as db:
            approved = db.execute("SELECT 1 FROM events WHERE proposal_id=? AND revision=? AND event='approve'",
                                  (source_proposal_id, original["revision"])).fetchone()
        if not approved or self.store.setting("product_completion_claim:" + str(cid)):
            raise Conflict("Original approval is absent or continuation was already attempted")
        live = self.inspect(cid)
        managed = live["managed"]
        if (not managed or managed.get("operation") != source_proposal_id or managed.get("complete") or managed.get("groups") or
                managed.get("plan") != original["body"].get("product_plan") or
                managed.get("feed_id") != original["body"]["before"].get("feed_id")):
            raise Conflict("Managed campaign is not the original empty partial draft")
        groups = self.source.entities("adgroups", "AdGroups", {"SelectionCriteria": {"CampaignIds": [cid]}, "FieldNames": ["Id"]}, allow_empty_result=True)
        if not inactive_draft(live["campaign"]) or groups or live["ads"]:
            raise Conflict("Campaign must still be an inactive draft without any groups or ads")
        plan = managed["plan"]
        purchase_strategy(live["campaign"])
        strategy = self.source.direct.build_unified_bidding_strategy(goal_id=PURCHASE_GOAL,
            cpa_micros=int(money(plan["target_cpa_rub"], "CPA") * 1000000),
            weekly_budget_micros=int(money(plan["weekly_budget_rub"], "budget") * 1000000), placement_type=plan["placement_type"])
        self.verify_strategy(live["campaign"], strategy)
        if live["campaign"]["Name"] != plan["name"]:
            raise Conflict("Campaign name changed outside the approved plan")
        return live

    def reconcile_creation(self, proposal_id):
        """Verify all recorded objects after a readback failure, without Direct writes."""
        row = self.store.get(proposal_id)
        action = row["body"]["action"]
        if row["state"] != "uncertain" or action.get("kind") not in ("product_create", "product_complete_create"):
            raise Conflict("Only an uncertain product creation can be reconciled")
        continued = action["kind"] == "product_complete_create"
        deployed = self.store.setting("product_deployed:" + action.get("draft_id", ""), {})
        cid = action.get("campaign_id") if continued else deployed.get("campaign_id")
        positive(cid, "campaign ID")
        claim_key = "product_completion_claim:" + str(cid) if continued else "product_draft_claim:" + action["draft_id"]
        if self.store.setting(claim_key, {}).get("operation") != proposal_id:
            raise Conflict("Creation claim does not match the inspected operation")
        live = self.inspect(cid)
        managed = live["managed"]
        plan = row["body"]["product_plan"]
        if (not managed or managed.get("complete") or managed.get("plan") != plan or
                managed.get("completion_operation" if continued else "operation") != proposal_id or
                managed.get("feed_id") != row["body"]["before"]["feed_id"]):
            raise Conflict("Managed objects do not match the approved creation")
        entries = managed["groups"]
        if len(entries) != len(plan["groups"]) or [g["index"] for g in entries] != list(range(len(entries))):
            raise Conflict("Not all planned groups have confirmed IDs")
        journal = self.operation(proposal_id)
        steps = journal.get("steps", [])
        expected = [] if continued else [("campaigns", ("add",), [cid])]
        for g in entries:
            expected += [("adgroups", ("add",), [g["group_id"]]), ("ads", ("add",), [g["ad_id"]]),
                         ("keywords", ("add", "update"), [g.get("autotargeting_id")]),
                         ("keywords", ("resume",), [g.get("autotargeting_id")])]
        if journal.get("target") != "production" or len(steps) != len(expected) or any(
            step.get("status") != "confirmed" or step.get("service") != service or step.get("method") not in methods or
            step.get("ids") != ids or any(type(i) is not int or i <= 0 for i in ids)
            for step, (service, methods, ids) in zip(steps, expected)
        ):
            raise Conflict("Creation journal contains missing, unconfirmed or unexpected writes")
        campaign, ads = live["campaign"], live["ads"]
        purchase_strategy(campaign)
        strategy = self.source.direct.build_unified_bidding_strategy(goal_id=PURCHASE_GOAL,
            cpa_micros=int(money(plan["target_cpa_rub"], "CPA") * 1000000),
            weekly_budget_micros=int(money(plan["weekly_budget_rub"], "budget") * 1000000), placement_type=plan["placement_type"])
        self.verify_strategy(campaign, strategy)
        if not inactive_draft(campaign) or campaign["Name"] != plan["name"]:
            raise Conflict("Campaign must match the approved inactive draft")
        if self.ready_feed(plan["feed_url"])["Id"] != managed["feed_id"]:
            raise Conflict("Feed readback mismatch")
        groups = self.source.entities("adgroups", "AdGroups", {"SelectionCriteria": {"CampaignIds": [cid]},
            "FieldNames": ["Id", "CampaignId", "Name", "RegionIds"], "UnifiedAdGroupFieldNames": ["OfferRetargeting"]})
        if {g["Id"] for g in groups} != {g["group_id"] for g in entries} or {a["Id"] for a in ads} != {g["ad_id"] for g in entries}:
            raise Conflict("Unexpected group or ad inventory; no reconciliation")
        for entry in entries:
            group_plan = plan["groups"][entry["index"]]
            group = next(g for g in groups if g["Id"] == entry["group_id"])
            ad = next(a for a in ads if a["Id"] == entry["ad_id"])
            if (group.get("CampaignId") != cid or group["Name"] != group_plan["name"] or
                    sorted(group["RegionIds"]) != sorted(plan["region_ids"]) or group.get("UnifiedAdGroup", {}).get("OfferRetargeting") != "NO" or
                    ad.get("CampaignId") != cid or ad.get("AdGroupId") != group["Id"] or ad.get("Type") != "SHOPPING_AD" or
                    ad.get("Status") != "DRAFT" or ad.get("State") not in ("OFF", "SUSPENDED")):
                raise Conflict("Group/ad readback differs from the approved inactive plan")
            self.verify_ad(ad, managed["feed_id"], group_plan)
            targeting = checked(self.source.direct.get_autotargeting_keywords(group["Id"])).get("Keywords", [])
            settings = {"Categories": {"Exact": "YES", "Narrow": "YES", "Alternative": "NO", "Accessory": "NO", "Broader": "NO"},
                        "BrandOptions": {"WithAdvertiserBrand": "YES", "WithoutBrands": "YES", "WithCompetitorsBrand": "NO"}}
            if len(targeting) != 1 or targeting[0].get("Id") != entry["autotargeting_id"] or targeting[0].get("State") != "ON" or targeting[0].get("AutotargetingSettings") != settings:
                raise Conflict("Autotargeting readback differs from the approved plan")
        result = {"campaign_id": cid, "feed_id": managed["feed_id"], "state": campaign["State"], "status": campaign["Status"],
                  "moderated": False, "old_campaign_changed": False, "continued_existing": continued, "reconciled": True}
        with self.store.db() as db:
            db.execute("BEGIN IMMEDIATE")
            current = self.store.unpack(db.execute("SELECT * FROM proposals WHERE id=?", (proposal_id,)).fetchone())
            approved = db.execute("SELECT 1 FROM events WHERE proposal_id=? AND revision=? AND event='approve'",
                                  (proposal_id, row["revision"])).fetchone()
            stored = db.execute("SELECT value FROM settings WHERE key=?", ("product_campaign:" + str(cid),)).fetchone()
            if current["state"] != "uncertain" or current["revision"] != row["revision"] or current["body"] != row["body"] or not approved or not stored or json.loads(stored[0]) != managed:
                raise Conflict("Unchanged original owner approval and managed state required")
            managed["complete"] = True
            journal.update(complete=True, result=result, reconciled=True)
            db.execute("UPDATE settings SET value=? WHERE key=?", (encode(managed), "product_campaign:" + str(cid)))
            db.execute("UPDATE settings SET value=? WHERE key=?", (encode(journal), "product_operation:" + proposal_id))
            now = time.time()
            db.execute("UPDATE proposals SET state='applied',result=?,updated=?,applied_at=? WHERE id=?", (encode(result), now, now, proposal_id))
            self.store.event(db, current, "product_creation_reconciled", "reconciler", result)
        return self.store.get(proposal_id)

    def inspect(self, cid):
        positive(cid, "campaign_id")
        campaign = self.source.campaign(cid)
        try:
            purchase_strategy(campaign)
            compliant, error = True, None
        except Conflict as exc:
            compliant, error = False, str(exc)
        return {"campaign": campaign, "ads": self.ads(cid), "managed": self.store.setting("product_campaign:" + str(cid)),
                "purchase_billing_compliant": compliant, "billing_error": error}

    def assert_goal(self):
        data = self.source.metrika.get_goals(COUNTER_ID)
        goal = next((g for g in data.get("goals", []) if g.get("id") == PURCHASE_GOAL), None)
        if not goal or goal.get("type") != "e_purchase":
            raise Conflict("Purchase goal must be confirmed by Metrika before creating or launching")

    def prepare(self, raw, snapshot=None):
        action = copy.deepcopy(raw.get("action", {}))
        kind = action.get("kind")
        if kind not in KINDS:
            raise ValueError("Unknown product action")
        plan, before, label, cid = None, {}, "", 0
        if kind == "product_feed_register":
            exact(action, {"kind", "feed_url"})
            catalog = self.fetcher(shop_url(action["feed_url"]))
            if catalog["invalid_count"] or catalog["unavailable"]:
                raise ValueError("В штатной выгрузке должны быть только корректные товары в наличии.")
            if any(f.get("UrlFeed", {}).get("Url") == action["feed_url"] for f in self.feeds()):
                raise Conflict("Фид с этим URL уже есть в Директе; проверьте его статус, не создавайте дубликат.")
            if self.store.setting("product_feed_claim:" + digest(action["feed_url"])):
                raise Conflict("Этот фид уже регистрировали; требуется сверка результата.")
            before = {"catalog_digest": digest(catalog["offers"])}
            label, title = "ArtFarfor", "Зарегистрировать автообновляемый товарный фид"
            details = f"Источник: {action['feed_url']}\nТоваров сейчас: {catalog['available']}.\nЭто не создаёт кампанию и не запускает рекламу."
        elif kind in ("product_create", "product_complete_create"):
            if kind == "product_create":
                exact(action, {"kind", "draft_id", "draft_revision"})
                positive(action["draft_revision"], "draft_revision")
                draft = self.get_draft(action["draft_id"])
                if draft["revision"] != action["draft_revision"]:
                    raise Conflict("Product draft revision changed")
                if self.store.setting("product_deployed:" + action["draft_id"]):
                    raise Conflict("This draft already has a created/partial campaign; inspect it instead of duplicating")
                if self.store.setting("product_draft_claim:" + action["draft_id"]):
                    raise Conflict("Создание по этому черновику уже запускалось; сначала сверка журнала.")
                plan = draft["plan"]
                cid = plan.get("replacement_for_campaign_id", 0)
            else:
                exact(action, {"kind", "campaign_id", "source_proposal_id"})
                cid = positive(action["campaign_id"], "campaign_id")
                live = self.empty_creation(cid, string(action["source_proposal_id"], "source_proposal_id", 12))
                plan = live["managed"]["plan"]
                before = {"campaign": live["campaign"], "ads": [], "groups": []}
            refreshed = self.validate(self.editable(plan), self.fetcher(plan["feed_url"]))
            if refreshed != plan:
                raise Conflict("Catalog changed; update the draft and review it again")
            self.assert_goal()
            before["feed_id"] = self.ready_feed(plan["feed_url"])["Id"]
            if kind == "product_complete_create" and before["feed_id"] != live["managed"]["feed_id"]:
                raise Conflict("The original campaign feed is no longer the verified ready feed")
            label = plan["name"]
            title = "Создать товарную кампанию без запуска" if kind == "product_create" else "Достроить товарную кампанию без запуска"
            details = (f"{label}\nОплата только за покупку: цель {PURCHASE_GOAL}.\n"
                       f"CPA: {plan['target_cpa_rub']} руб.; недельный бюджет: {plan['weekly_budget_rub']} руб.\n"
                       f"Размещение: {plan['placement_type']}; регионы: {plan['region_ids']}.\n"
                       f"Групп: {len(plan['groups'])}; товаров сейчас: {sum(g['product_count'] for g in plan['groups'])}.\n"
                       "Автотаргетинг: целевые и узкие запросы; без конкурентов. Офферный ретаргетинг выключен.\n"
                       "Кампания и объявления останутся черновиками без показов. Модерация и запуск не выполняются.")
            if kind == "product_complete_create":
                details += f"\nИспользуется существующая кампания {cid}. Новая кампания не создаётся."
        else:
            allowed = {"kind", "campaign_id"}
            if kind in ("product_edit", "product_ad_edit"):
                allowed.add("patch")
            if kind == "product_ad_edit":
                allowed.add("ad_id")
            if kind == "product_pause_old":
                allowed.add("replacement_campaign_id")
            exact(action, allowed)
            cid = positive(action["campaign_id"], "campaign_id")
            live = self.inspect(cid)
            campaign, ads = live["campaign"], live["ads"]
            label = campaign["Name"]
            before = {"campaign": campaign, "ads": ads}
            managed = live["managed"]
            if kind != "product_pause_old":
                purchase_strategy(campaign)
                if not managed or not managed.get("complete"):
                    raise Conflict("Only fully created bot-managed product campaigns can be edited/launched")
                plan = copy.deepcopy(managed["plan"])
                expected_strategy = self.source.direct.build_unified_bidding_strategy(
                    goal_id=PURCHASE_GOAL, cpa_micros=int(money(plan["target_cpa_rub"], "CPA") * 1000000),
                    weekly_budget_micros=int(money(plan["weekly_budget_rub"], "budget") * 1000000), placement_type=plan["placement_type"])
                self.verify_strategy(campaign, expected_strategy)
                expected_ids = {g["ad_id"] for g in managed["groups"]}
                if {a["Id"] for a in ads} != expected_ids:
                    raise Conflict("Состав объявлений изменён вне бота; сначала сверка структуры.")
                for entry in managed["groups"]:
                    self.verify_ad(next(a for a in ads if a["Id"] == entry["ad_id"]), managed["feed_id"], plan["groups"][entry["index"]])
                if campaign["Name"] != plan["name"]:
                    raise Conflict("Название изменено вне бота; сначала сверка структуры.")
                for side in purchase_strategy(campaign):
                    actual_values = campaign["UnifiedCampaign"]["BiddingStrategy"][side]["PayForConversion"]
                    if any(actual_values[key] != int(money(plan[field], field) * 1000000) for key, field in
                           (("Cpa", "target_cpa_rub"), ("WeeklySpendLimit", "weekly_budget_rub"))):
                        raise Conflict("Стратегия изменена вне бота; сначала сверка структуры.")
            if kind in ("product_edit", "product_ad_edit"):
                patch = action["patch"]
                fields = {"name", "target_cpa_rub", "weekly_budget_rub"} if kind == "product_edit" else {"default_text", "category_ids", "offer_ids"}
                if not isinstance(patch, dict) or len(patch) != 1 or set(patch) - fields:
                    raise ValueError("One independently approved field per edit card")
                editable = self.editable(plan)
                if kind == "product_ad_edit":
                    aid = positive(action["ad_id"], "ad_id")
                    match = next((g for g in managed["groups"] if g["ad_id"] == aid), None)
                    if not match or not any(a["Id"] == aid and a.get("ShoppingAd") for a in ads):
                        raise Conflict("Ad does not belong to this managed product campaign")
                    editable["groups"][match["index"]].update(patch)
                else:
                    editable.update(patch)
                changed = self.validate(editable, self.fetcher(plan["feed_url"]))
                if changed == plan:
                    raise ValueError("No change")
                plan = changed
                title = "Изменить товарное объявление" if kind == "product_ad_edit" else "Изменить параметр товарной кампании"
                details = f"Только кампания {cid}" + (f", объявление {action['ad_id']}" if kind == "product_ad_edit" else "") + ":\n" + json.dumps(patch, ensure_ascii=False)
            elif kind == "product_moderate":
                if inactive_draft(campaign) and campaign["State"] == "OFF":
                    raise Conflict("Автомодерация черновика пока заблокирована: сначала нужно подтвердить способ удержать показы выключенными после модерации. Создание не разрешает запуск.")
                if campaign["State"] != "SUSPENDED" or not ads or any(a["Status"] != "DRAFT" for a in ads):
                    raise Conflict("Moderation is separate: campaign must be suspended and all ads must be drafts")
                title, details = "Отправить товарные объявления на модерацию", f"Объявления кампании {cid}. Кампания останется остановленной."
            elif kind == "product_launch":
                if campaign["State"] != "SUSPENDED" or not ads or any(a["Status"] not in ("ACCEPTED", "PREACCEPTED") for a in ads):
                    raise Conflict("Launch requires a suspended campaign and accepted ads; wait for moderation")
                self.assert_goal()
                if self.ready_feed(plan["feed_url"])["Id"] != managed["feed_id"]:
                    raise Conflict("Не подтверждён готовый фид кампании.")
                title, details = "Запустить товарную кампанию", f"Запустить показы кампании {cid}. Оплата только за цель покупки {PURCHASE_GOAL}. Старую кампанию не останавливать.\nПеред одобрением проверьте в InSales правильность складов и исключение отсутствующих товаров из выгрузки."
            else:
                replacement = self.inspect(positive(action["replacement_campaign_id"], "replacement_campaign_id"))
                linked = replacement["managed"] or {}
                if (not linked.get("complete") or linked.get("plan", {}).get("replacement_for_campaign_id") != cid or
                        replacement["campaign"]["State"] != "ON" or not replacement["purchase_billing_compliant"]):
                    raise Conflict("Replacement is not linked, active or purchase-billed; old campaign will not be stopped")
                if campaign["State"] != "ON":
                    raise Conflict("Original campaign is not active")
                before["replacement_id"] = action["replacement_campaign_id"]
                title, details = "Остановить прежнюю кампанию", f"Только кампания {cid}. Товарная замена: {action['replacement_campaign_id']}. Это отдельное решение владельца, а не автоматическое следствие теста."
        reason = raw.get("reason") or "Подготовлен товарный сценарий для проверки владельцем; результат продаж не гарантирован."
        body = {"campaign_id": cid, "campaign_name": label, "area": "experiment", "title": title,
                "reason": string(reason, "reason", 1600), "expected_effect": "Проверить товарный формат на реальных покупках, отдельно от корзин и кликов.",
                "success_metric": "Проверены настройки и результат API. Эффективность оценивается отдельно по зрелой статистике покупки и данным об оплатах.",
                "evaluate_after_days": 28, "action": action, "before": before, "product_plan": plan,
                "product_details": details,
                "evidence": {"product_workflow": True, "checked_at": time.time(), "goal_id": PURCHASE_GOAL,
                             "limits": ["Ecommerce-покупка не доказывает оплату заказа; корзины не являются покупками.",
                                        "Фид обновляется Яндексом с задержкой. Наличие должно поддерживаться в InSales.",
                                        "Полная цепочка не проверена в sandbox: создание ЕПК возвращает NotImplemented. Первый запуск требует проверки владельца."]},
                "warnings": ["Изменения только после вашего одобрения. Другие карточки не применяются автоматически."]}
        if plan:
            body["warnings"].append("Автообновляемый фид: товары, цены и наличие внутри выбранных категорий меняются по данным InSales; нейросеть их не выдумывает.")
        from marketer.telegram import card
        card({"id": "0" * 12, "revision": 1, "state": "pending", "body": body})
        return body

    def execute(self, row):
        try:
            return self._execute(row)
        except Conflict as exc:
            if self.operation(row["id"]).get("steps"):
                raise RuntimeError("После запроса на изменение нужна сверка результата: " + str(exc)) from None
            raise

    def _execute(self, row):
        body, action = row["body"], row["body"]["action"]
        kind, cid, plan = action["kind"], body["campaign_id"], body.get("product_plan")
        journal_key = "product_operation:" + row["id"]
        if self.store.setting(journal_key):
            raise Conflict("Operation already attempted; inspect its journal, never retry automatically")
        # Rebuild the proposal from fresh reads before the first production write.
        fresh = self.prepare({"action": action, "reason": body["reason"]})
        if fresh["before"] != body["before"] or fresh["product_plan"] != plan:
            raise Conflict("Settings or catalog changed since approval; create a fresh card")
        journal = {"proposal_id": row["id"], "steps": [], "started": time.time(), "target": "production"}
        self.store.set_setting(journal_key, journal)

        def write(label, service, method, params):
            step = {"label": label, "status": "requested", "service": service, "method": method}
            journal["steps"].append(step)
            self.store.set_setting(journal_key, journal)
            result = checked(self.source.direct.call_v501(service, method, params).body)
            items = result.get(method.capitalize() + "Results", [])
            expected = len(params.get("SelectionCriteria", {}).get("Ids", [])) or len(next(iter(params.values())))
            if len(items) != expected or any(i.get("Errors") or not i.get("Id") for i in items):
                step.update(status="rejected_or_partial", result=result)
                self.store.set_setting(journal_key, journal)
                errors = [e for item in items for e in item.get("Errors", [])]
                reason = "; ".join(f"{e.get('Details') or e.get('Message') or 'Ошибка'} (код {e.get('Code', '?')})" for e in errors)
                raise RuntimeError("Яндекс.Директ: " + (reason[:600] or "не подтвердил полный результат запроса") + ". Выполнение остановлено; нужна сверка уже созданных объектов.")
            if method != "add":
                expected_ids = params.get("SelectionCriteria", {}).get("Ids") or [i["Id"] for i in next(iter(params.values()))]
                if [i["Id"] for i in items] != expected_ids:
                    raise RuntimeError("Unexpected IDs in write response; inspect operation journal")
            step.update(status="confirmed", ids=[i["Id"] for i in items])
            self.store.set_setting(journal_key, journal)
            return step["ids"]

        if kind == "product_feed_register":
            self.claim("product_feed_claim:" + digest(action["feed_url"]), row["id"])
            feed_id = write("feed", "feeds", "add", {"Feeds": [{"Name": "ArtFarfor native inventory", "BusinessType": "RETAIL", "SourceType": "URL", "UrlFeed": {"Url": action["feed_url"], "RemoveUtmTags": "YES"}}]})[0]
            observed = next((f for f in self.feeds() if f["Id"] == feed_id), {})
            if not native_feed_matches(observed, action["feed_url"], allow_pending=True):
                raise RuntimeError("Feed registration readback mismatch; do not retry")
            result = {"feed_id": feed_id, "processing_status": observed.get("Status"), "campaign_created": False}
        elif kind in ("product_create", "product_complete_create"):
            feed_id = body["before"]["feed_id"]
            strategy = self.source.direct.build_unified_bidding_strategy(goal_id=PURCHASE_GOAL, cpa_micros=int(money(plan["target_cpa_rub"], "CPA") * 1000000), weekly_budget_micros=int(money(plan["weekly_budget_rub"], "budget") * 1000000), placement_type=plan["placement_type"])
            if kind == "product_create":
                self.claim("product_draft_claim:" + action["draft_id"], row["id"])
                campaign = {"Name": plan["name"], "StartDate": datetime.now(ZoneInfo("Europe/Moscow")).date().isoformat(),
                            "TimeZone": "Europe/Moscow", "UnifiedCampaign": {"CounterIds": {"Items": [COUNTER_ID]}, "BiddingStrategy": strategy,
                            "TrackingParams": "utm_source=yandex&utm_medium=cpc&utm_campaign={campaign_id}&utm_content={ad_id}&utm_term={keyword}"}}
                created_id = write("campaign", "campaigns", "add", {"Campaigns": [campaign]})[0]
                self.store.set_setting("product_deployed:" + action["draft_id"], {"campaign_id": created_id, "operation": row["id"]})
                managed = {"campaign_id": created_id, "feed_id": feed_id, "plan": plan, "groups": [], "complete": False, "operation": row["id"]}
            else:
                self.claim("product_completion_claim:" + str(cid), row["id"])
                created_id = cid
                managed = self.store.setting("product_campaign:" + str(cid))
                managed["completion_operation"] = row["id"]
            key = "product_campaign:" + str(created_id)
            self.store.set_setting(key, managed)
            observed = self.source.campaign(created_id)
            purchase_strategy(observed)
            if not inactive_draft(observed):
                raise RuntimeError("Кампания не является выключенным черновиком; объявления не создавались.")
            self.verify_strategy(observed, strategy)
            for index, group in enumerate(plan["groups"]):
                gid = write("group " + str(index), "adgroups", "add", {"AdGroups": [{"Name": group["name"], "CampaignId": created_id, "RegionIds": plan["region_ids"], "UnifiedAdGroup": {"OfferRetargeting": "NO"}}]})[0]
                entry = {"index": index, "group_id": gid, "ad_id": None}
                managed["groups"].append(entry)
                self.store.set_setting(key, managed)
                aid = write("shopping ad " + str(index), "ads", "add", {"Ads": [{"AdGroupId": gid, "ShoppingAd": {"FeedId": feed_id, "DefaultTexts": [group["default_text"]], "FeedFilterConditions": group["filters"]}}]})[0]
                entry["ad_id"] = aid
                self.store.set_setting(key, managed)
                autotargets = checked(self.source.direct.get_autotargeting_keywords(gid)).get("Keywords", [])
                if len(autotargets) > 1:
                    raise RuntimeError("Unexpected multiple autotargetings; campaign is not sent for moderation")
                settings = {"Categories": {"Exact": "YES", "Narrow": "YES", "Alternative": "NO", "Accessory": "NO", "Broader": "NO"},
                            "BrandOptions": {"WithAdvertiserBrand": "YES", "WithoutBrands": "YES", "WithCompetitorsBrand": "NO"}}
                if autotargets:
                    tid = autotargets[0]["Id"]
                    write("targeting settings", "keywords", "update", {"Keywords": [{"Id": tid, "AutotargetingSettings": settings}]})
                else:
                    tid = write("targeting", "keywords", "add", {"Keywords": [{"Keyword": "---autotargeting", "AdGroupId": gid, "AutotargetingSettings": settings}]})[0]
                write("enable targeting", "keywords", "resume", {"SelectionCriteria": {"Ids": [tid]}})
                entry["autotargeting_id"] = tid
                self.store.set_setting(key, managed)
                targeting = checked(self.source.direct.get_autotargeting_keywords(gid)).get("Keywords", [])
                if len(targeting) != 1 or targeting[0].get("Id") != tid or targeting[0].get("State") != "ON" or targeting[0].get("AutotargetingSettings") != settings:
                    raise RuntimeError("Autotargeting readback mismatch; campaign is not sent for moderation")
            actual = self.ads(created_id)
            for group in managed["groups"]:
                ad = next((a for a in actual if a["Id"] == group["ad_id"]), {})
                expected = plan["groups"][group["index"]]
                self.verify_ad(ad, feed_id, expected)
                if ad.get("Status") != "DRAFT":
                    raise RuntimeError("New ad is not a draft; campaign state requires inspection")
            observed = self.source.campaign(created_id)
            purchase_strategy(observed)
            if observed["Name"] != plan["name"] or not inactive_draft(observed):
                raise RuntimeError("New campaign settings readback mismatch")
            self.verify_strategy(observed, strategy)
            managed["complete"] = True
            self.store.set_setting(key, managed)
            result = {"campaign_id": created_id, "feed_id": feed_id, "state": observed["State"], "status": observed["Status"],
                      "moderated": False, "old_campaign_changed": False, "continued_existing": kind == "product_complete_create"}
        else:
            live = self.inspect(cid)
            managed = live["managed"]
            if kind == "product_edit":
                patch = action["patch"]
                payload = {"Id": cid}
                if "name" in patch:
                    payload["Name"] = plan["name"]
                else:
                    strategy = copy.deepcopy(live["campaign"]["UnifiedCampaign"]["BiddingStrategy"])
                    field = "Cpa" if "target_cpa_rub" in patch else "WeeklySpendLimit"
                    value = int(money(next(iter(patch.values())), "strategy value") * 1000000)
                    for side in purchase_strategy(live["campaign"]):
                        strategy[side]["PayForConversion"][field] = value
                    payload["UnifiedCampaign"] = {"BiddingStrategy": strategy}
                write("edit campaign", "campaigns", "update", {"Campaigns": [payload]})
                observed = self.source.campaign(cid)
                purchase_strategy(observed)
                if "name" in patch and observed["Name"] != plan["name"]:
                    raise RuntimeError("Campaign name readback mismatch")
                if "name" not in patch and observed["UnifiedCampaign"]["BiddingStrategy"] != strategy:
                    raise RuntimeError("Campaign strategy readback mismatch")
                managed["plan"] = plan
                self.store.set_setting("product_campaign:" + str(cid), managed)
            elif kind == "product_ad_edit":
                entry = next(g for g in managed["groups"] if g["ad_id"] == action["ad_id"])
                group = plan["groups"][entry["index"]]
                field = "DefaultTexts" if "default_text" in action["patch"] else "FeedFilterConditions"
                value = [group["default_text"]] if field == "DefaultTexts" else group["filters"]
                write("edit product ad", "ads", "update", {"Ads": [{"Id": action["ad_id"], "ShoppingAd": {field: value}}]})
                observed = next(a for a in self.ads(cid) if a["Id"] == action["ad_id"])
                self.verify_ad(observed, managed["feed_id"], group)
                managed["plan"] = plan
                self.store.set_setting("product_campaign:" + str(cid), managed)
            elif kind == "product_moderate":
                ids = [a["Id"] for a in live["ads"]]
                write("moderate product ads", "ads", "moderate", {"SelectionCriteria": {"Ids": ids}})
                observed = self.ads(cid)
                if not observed or any(a["Status"] not in ("MODERATION", "ACCEPTED", "PREACCEPTED") for a in observed) or self.source.campaign(cid)["State"] != "SUSPENDED":
                    raise RuntimeError("Moderation or suspended state could not be verified")
            else:
                method, expected_state = ("resume", "ON") if kind == "product_launch" else ("suspend", "SUSPENDED")
                if kind == "product_launch":
                    # Inventory may have changed while ads were on moderation.
                    self.validate(self.editable(plan), self.fetcher(plan["feed_url"]))
                write(method, "campaigns", method, {"SelectionCriteria": {"Ids": [cid]}})
                if self.source.campaign(cid)["State"] != expected_state:
                    raise RuntimeError("Campaign state readback mismatch")
                if kind == "product_launch":
                    purchase_strategy(self.source.campaign(cid))
            result = {"campaign_id": cid, "operation": kind, "verified": True}
        journal["complete"] = True
        journal["result"] = result
        self.store.set_setting(journal_key, journal)
        return result

    @staticmethod
    def verify_strategy(campaign, expected):
        actual = campaign.get("UnifiedCampaign", {}).get("BiddingStrategy", {})
        for side, values in expected.items():
            observed = actual.get(side, {})
            for key, wanted in values.items():
                if isinstance(wanted, dict):
                    if any(observed.get(key, {}).get(k) != v for k, v in wanted.items()):
                        raise Conflict("Strategy/placement readback differs from approved settings")
                elif observed.get(key) != wanted:
                    raise Conflict("Strategy readback differs from approved settings")

    @staticmethod
    def verify_ad(ad, feed_id, group):
        fields = ad.get("ShoppingAd", {})
        def filters(values):
            if isinstance(values, dict) and set(values) == {"Items"}:
                values = values["Items"]
            if not isinstance(values, list) or any(not isinstance(f, dict) or not isinstance(f.get("Operand"), str) or
                    not isinstance(f.get("Operator"), str) or not isinstance(f.get("Arguments"), list) or
                    any(not isinstance(a, str) for a in f["Arguments"]) for f in values):
                raise RuntimeError("Некорректный формат фильтров в ответе Яндекс.Директа; требуется сверка.")
            return sorted((f["Operand"], f["Operator"], tuple(sorted(f["Arguments"]))) for f in values)
        if (fields.get("FeedId") != feed_id or fields.get("DefaultTexts") != [group["default_text"]] or
                filters(fields.get("FeedFilterConditions", [])) != filters(group["filters"])):
            raise RuntimeError("ShoppingAd readback mismatch")
