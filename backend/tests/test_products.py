import copy
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from marketer.actions import Actions
from marketer.data import DataSource
from marketer.catalog import parse_yml, select_offers, shop_url
from marketer.products import COUNTER_ID, PURCHASE_GOAL, Products, purchase_strategy, replacement_candidates
from marketer.runner import Runner
from marketer.store import Conflict, Store
from marketer.telegram import card, detail_pages, telegram_length
from services.direct_client import YandexDirectClient, YandexDirectClientError
from services.direct_mock import validate_campaign


URL = "https://artfarfor.com/test-feed.xml"


def xml(available="true", extra="", declaration='<!DOCTYPE yml_catalog SYSTEM "shops.dtd">'):
    date = datetime.now(timezone.utc).isoformat()
    return (f'{declaration}<yml_catalog date="{date}"><shop><url>https://artfarfor.com</url>'
            '<categories><category id="1">Porcelain</category><category id="2" parentId="1">Clowns</category></categories>'
            f'<offers><offer id="11" available="{available}"><name>Clown</name><url>https://artfarfor.com/product/clown</url>'
            '<price>15000.0</price><currencyId>RUB</currencyId><categoryId>2</categoryId>'
            f'<picture>https://cdn.insales-shop.ru/clown.jpg</picture></offer>{extra}</offers></shop></yml_catalog>').encode()


def draft():
    return {"name": "Product pilot", "feed_url": URL, "placement_type": "search_only", "region_ids": [225],
            "target_cpa_rub": "1000", "weekly_budget_rub": "20000",
            "groups": [{"name": "Clowns", "category_ids": ["2"], "default_text": "Collectible porcelain"}]}


class FakeDirect:
    build_unified_bidding_strategy = YandexDirectClient.build_unified_bidding_strategy
    normalize_placement_type = YandexDirectClient.normalize_placement_type
    PLACEMENT_SEARCH_ONLY = "search_only"
    PLACEMENT_NETWORK_ONLY = "network_only"
    PLACEMENT_BOTH = "both"
    DEFAULT_SEARCH_PLACEMENT_TYPES = YandexDirectClient.DEFAULT_SEARCH_PLACEMENT_TYPES
    DEFAULT_NETWORK_PLACEMENT_TYPES = YandexDirectClient.DEFAULT_NETWORK_PLACEMENT_TYPES

    def __init__(self):
        self.direct = self
        self.metrika = SimpleNamespace(get_goals=lambda cid: {"goals": [{"id": PURCHASE_GOAL, "type": "e_purchase"}]})
        self.items = {"feeds": [{"Id": 5, "SourceType": "URL", "BusinessType": "RETAIL", "Status": "DONE", "NumberOfItems": 1, "UrlFeed": {"Url": URL}}],
                      "campaigns": [], "adgroups": [], "ads": [], "keywords": []}
        self.writes, self.next_id, self.fail = [], 100, None
        self.feed_readback = {}

    def entities(self, service, key, params, allow_empty_result=False):
        criteria = params.get("SelectionCriteria", {})
        values = self.items[service]
        if "CampaignIds" in criteria:
            values = [v for v in values if v["CampaignId"] in criteria["CampaignIds"]]
        values = copy.deepcopy(values)
        if service == "ads":
            for value in values:
                fields = value.get("ShoppingAd", {})
                if isinstance(fields.get("FeedFilterConditions"), list):
                    fields["FeedFilterConditions"] = {"Items": fields["FeedFilterConditions"]}
        return values

    def campaign(self, cid):
        return copy.deepcopy(next(c for c in self.items["campaigns"] if c["Id"] == cid))

    def get_autotargeting_keywords(self, gid):
        return {"result": {"Keywords": copy.deepcopy([k for k in self.items["keywords"] if k["AdGroupId"] == gid])}}

    def call_v501(self, service, method, params):
        self.writes.append((service, method, copy.deepcopy(params)))
        if self.fail == (service, method):
            raise TimeoutError("unknown result")
        results = []
        if method == "add":
            for value in next(iter(params.values())):
                self.next_id += 1
                item = copy.deepcopy(value) | {"Id": self.next_id}
                if service == "campaigns":
                    item.update(Type="UNIFIED_CAMPAIGN", State="OFF", Status="DRAFT")
                if service == "ads":
                    group = next(g for g in self.items["adgroups"] if g["Id"] == item["AdGroupId"])
                    item.update(CampaignId=group["CampaignId"], Type="SHOPPING_AD", State="OFF", Status="DRAFT")
                if service == "feeds":
                    item.update(Status="NEW", NumberOfItems=0)
                    item.update(copy.deepcopy(self.feed_readback))
                if service == "keywords":
                    item.update(State="ON")
                self.items[service].append(item)
                results.append({"Id": item["Id"]})
        elif method == "update":
            for change in next(iter(params.values())):
                item = next(i for i in self.items[service] if i["Id"] == change["Id"])
                for key, value in change.items():
                    if key in ("ShoppingAd", "UnifiedCampaign"):
                        item[key].update(copy.deepcopy(value))
                    else:
                        item[key] = copy.deepcopy(value)
                results.append({"Id": item["Id"]})
        else:
            for identifier in params["SelectionCriteria"]["Ids"]:
                item = next(i for i in self.items[service] if i["Id"] == identifier)
                if service == "campaigns" and method == "suspend" and item["Status"] == "DRAFT":
                    results.append({"Errors": [{"Code": 8300, "Details": "Кампания является черновиком и не может быть остановлена"}]})
                    continue
                if method == "moderate":
                    item["Status"] = "MODERATION"
                else:
                    item["State"] = "ON" if method == "resume" else "SUSPENDED"
                results.append({"Id": identifier})
        return SimpleNamespace(body={"result": {method.capitalize() + "Results": results}})


class CatalogTests(unittest.TestCase):
    def test_standard_insales_dtd_does_not_load_external_resource(self):
        result = parse_yml(xml(), URL)
        self.assertEqual((result["total"], result["available"], result["invalid_count"]), (1, 1, 0))
        offers, filters = select_offers(result, ["1"], [])
        self.assertEqual(offers[0]["id"], "11")
        self.assertEqual(filters[0]["Arguments"], ["1", "2"])

    def test_entities_and_arbitrary_dtd_rejected(self):
        for declaration in ('<!DOCTYPE yml_catalog [<!ENTITY x "bad">]>', '<!DOCTYPE yml_catalog SYSTEM "https://evil.test/x">'):
            with self.assertRaises(ValueError):
                parse_yml(xml(declaration=declaration), URL)

    def test_stale_and_wrong_host_rejected(self):
        with self.assertRaises(ValueError):
            parse_yml(xml(), URL, now=time.time() + 3 * 86400)
        for url in ("http://artfarfor.com/x", "https://localhost/x", "https://artfarfor.com@evil.test/x", "https://artfarfor.com:22/x"):
            with self.assertRaises(ValueError):
                shop_url(url)

    def test_unknown_inventory_and_category_rejected(self):
        with self.assertRaises(ValueError):
            parse_yml(xml(available=""), URL)

    def test_unavailable_cannot_be_selected(self):
        with self.assertRaises(ValueError):
            select_offers(parse_yml(xml(available="false"), URL), ["1"], [])


class ProductTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "state.sqlite")
        self.source = FakeDirect()
        self.products = Products(self.source, self.store, lambda url: parse_yml(xml(), url))
        self.actions = Actions(self.source, self.store, {})
        self.actions.products = self.products

    def tearDown(self):
        self.tmp.cleanup()

    def propose(self, action):
        body = self.actions.prepare({"action": action}, None)
        return self.store.create(body)[0]

    def approve_fake(self, row):
        # Unit-test actor, isolated SQLite and fake Direct. Never a production identity.
        return self.actions.decide(row["id"], row["revision"], "approve", "test-owner")

    def create(self):
        saved = self.products.save_draft(draft())
        row = self.propose({"kind": "product_create", "draft_id": saved["id"], "draft_revision": saved["revision"]})
        return saved, row, self.approve_fake(row)

    def test_draft_and_card_never_write_direct(self):
        saved = self.products.save_draft(draft())
        row = self.propose({"kind": "product_create", "draft_id": saved["id"], "draft_revision": 1})
        self.assertEqual(self.source.writes, [])
        self.assertLess(telegram_length(card(row)), 4001)
        self.assertIn("Clowns", "".join(detail_pages(row)))

    def test_full_create_stops_before_moderation_and_launch(self):
        saved, row, result = self.create()
        self.assertEqual(result["state"], "applied", result.get("result"))
        cid = result["result"]["campaign_id"]
        self.assertEqual(self.source.campaign(cid)["State"], "OFF")
        self.assertEqual(result["result"]["status"], "DRAFT")
        self.assertFalse(any(service == "campaigns" and method == "suspend" for service, method, _ in self.source.writes))
        self.assertTrue(self.products.inspect(cid)["managed"]["complete"])
        self.assertFalse(any(service == "campaigns" and method == "resume" or method == "moderate" for service, method, _ in self.source.writes))
        count = len(self.source.writes)
        self.approve_fake(row)
        self.assertEqual(count, len(self.source.writes))
        with self.assertRaises(Conflict):
            self.propose({"kind": "product_create", "draft_id": saved["id"], "draft_revision": 1})

    def test_unknown_campaign_write_reserves_draft_across_revisions(self):
        self.source.fail = ("campaigns", "add")
        saved, row, result = self.create()
        self.assertEqual(result["state"], "uncertain")
        self.assertEqual(self.products.operation(row["id"])["steps"][0]["status"], "requested")
        revised = self.products.save_draft(draft(), saved["id"], 1)
        with self.assertRaises(Conflict):
            self.propose({"kind": "product_create", "draft_id": saved["id"], "draft_revision": revised["revision"]})
        self.assertEqual(len(self.source.writes), 1)

    def legacy_partial_creation(self):
        saved = self.products.save_draft(draft())
        original = self.propose({"kind": "product_create", "draft_id": saved["id"], "draft_revision": 1})
        self.store.decide(original["id"], 1, "approve", "test-owner")
        plan = saved["plan"]
        strategy = self.source.build_unified_bidding_strategy(goal_id=PURCHASE_GOAL, cpa_micros=1000000000,
                                                             weekly_budget_micros=20000000000, placement_type="search_only")
        result = self.source.call_v501("campaigns", "add", {"Campaigns": [{"Name": plan["name"],
            "UnifiedCampaign": {"BiddingStrategy": strategy, "CounterIds": {"Items": [COUNTER_ID]}}}]}).body["result"]
        cid = result["AddResults"][0]["Id"]
        stopped = self.source.call_v501("campaigns", "suspend", {"SelectionCriteria": {"Ids": [cid]}}).body["result"]
        self.products.claim("product_draft_claim:" + saved["id"], original["id"])
        self.store.set_setting("product_deployed:" + saved["id"], {"campaign_id": cid, "operation": original["id"]})
        self.store.set_setting("product_campaign:" + str(cid), {"campaign_id": cid, "feed_id": 5, "plan": plan,
                               "groups": [], "complete": False, "operation": original["id"]})
        self.store.set_setting("product_operation:" + original["id"], {"target": "production", "steps": [
            {"service": "campaigns", "method": "add", "status": "confirmed", "ids": [cid]},
            {"service": "campaigns", "method": "suspend", "status": "rejected_or_partial", "result": stopped}]})
        self.store.finish(original["id"], "uncertain", {"error": "legacy suspension failure"})
        return saved, original, {"kind": "product_complete_create", "campaign_id": cid, "source_proposal_id": original["id"]}

    def test_complete_empty_campaign_requires_new_card_and_never_repeats_add(self):
        _, original, action = self.legacy_partial_creation()
        count = len(self.source.writes)
        continuation = self.propose(action)
        self.assertEqual(len(self.source.writes), count)
        result = self.approve_fake(continuation)
        self.assertEqual(result["state"], "applied", result["result"])
        self.assertEqual(result["result"]["campaign_id"], action["campaign_id"])
        self.assertEqual(result["result"]["state"], "OFF")
        self.assertTrue(result["result"]["continued_existing"])
        self.assertEqual(sum(service == "campaigns" and method == "add" for service, method, _ in self.source.writes), 1)
        self.assertEqual(self.store.get(original["id"])["state"], "uncertain")
        with self.assertRaises(Conflict):
            self.propose(action)

    def test_continuation_rejects_existing_group_and_external_campaign_changes(self):
        _, _, action = self.legacy_partial_creation()
        self.source.items["adgroups"] = [{"Id": 999, "CampaignId": action["campaign_id"]}]
        with self.assertRaises(Conflict):
            self.propose(action)
        self.source.items["adgroups"] = []
        original = copy.deepcopy(self.source.items["campaigns"][0])
        for change in ({"State": "ON"}, {"Status": "ACCEPTED"}, {"Name": "Changed"}):
            self.source.items["campaigns"] = [original | change]
            with self.assertRaises(Conflict):
                self.propose(action)
        self.assertEqual(len(self.source.writes), 2)

    def test_inspected_partial_card_can_propose_continuation_but_cannot_replay(self):
        _, original, action = self.legacy_partial_creation()
        self.store.finish(original["id"], "partial", {"campaign_id": action["campaign_id"], "error": "Verified empty draft"})
        row = self.propose(action)
        self.assertEqual(row["state"], "pending")
        count = len(self.source.writes)
        self.approve_fake(original)
        self.assertEqual(len(self.source.writes), count)
        self.assertIn("Частично создано", card(self.store.get(original["id"])))
        self.assertIn(str(action["campaign_id"]), card(self.store.get(original["id"])))

    def test_continuation_blocks_changed_draft_and_missing_original_approval(self):
        saved, original, action = self.legacy_partial_creation()
        with self.store.db() as db:
            db.execute("DELETE FROM events WHERE proposal_id=? AND event='approve'", (original["id"],))
        with self.assertRaises(Conflict):
            self.propose(action)
        self.products.save_draft(draft() | {"name": "Changed"}, saved["id"], 1)
        with self.assertRaises(Conflict):
            self.propose(action)

    def test_continuation_partial_failure_cannot_be_replayed(self):
        _, _, action = self.legacy_partial_creation()
        row = self.propose(action)
        self.source.fail = ("adgroups", "add")
        result = self.approve_fake(row)
        self.assertEqual(result["state"], "uncertain")
        with self.assertRaises(Conflict):
            self.propose(action)
        count = len(self.source.writes)
        self.approve_fake(row)
        self.assertEqual(len(self.source.writes), count)

    def test_unrecognized_partial_creation_cannot_continue(self):
        _, original, action = self.legacy_partial_creation()
        journal = self.products.operation(original["id"])
        journal["steps"][1]["status"] = "requested"
        self.store.set_setting("product_operation:" + original["id"], journal)
        with self.assertRaises(Conflict):
            self.propose(action)

    def test_moderation_of_inactive_draft_is_not_automatic_launch(self):
        _, _, result = self.create()
        count = len(self.source.writes)
        with self.assertRaises(Conflict):
            self.propose({"kind": "product_moderate", "campaign_id": result["result"]["campaign_id"]})
        self.assertEqual(len(self.source.writes), count)

    def test_empty_ads_api_result_is_opt_in_not_any_malformed_result(self):
        direct = Mock()
        direct.call_v501.return_value.body = {"result": {}}
        source = DataSource({}, direct=direct, metrika=Mock())
        self.assertEqual(source.entities("ads", "Ads", {}, allow_empty_result=True), [])
        with self.assertRaises(ValueError):
            source.entities("ads", "Ads", {})
        direct.call_v501.return_value.body = {"result": {"LimitedBy": 1000}}
        with self.assertRaises(ValueError):
            source.entities("ads", "Ads", {}, allow_empty_result=True)

    def test_creation_card_does_not_display_zero_campaign_id(self):
        saved = self.products.save_draft(draft())
        row = self.propose({"kind": "product_create", "draft_id": saved["id"], "draft_revision": 1})
        self.assertNotIn("Product pilot (0)", card(row))
        self.assertNotIn("Product pilot (0)", "".join(detail_pages(row)))

    def test_creation_readback_reconciliation_performs_no_writes(self):
        with patch.object(self.products, "verify_ad", side_effect=RuntimeError("legacy readback failure")):
            saved, row, result = self.create()
        self.assertEqual(result["state"], "uncertain")
        count = len(self.source.writes)
        recovered = self.products.reconcile_creation(row["id"])
        self.assertEqual(recovered["state"], "applied")
        self.assertTrue(recovered["result"]["reconciled"])
        self.assertEqual(recovered["result"]["state"], "OFF")
        self.assertEqual(len(self.source.writes), count)
        self.assertTrue(self.products.inspect(recovered["result"]["campaign_id"])["managed"]["complete"])
        with self.assertRaises(Conflict):
            self.propose({"kind": "product_create", "draft_id": saved["id"], "draft_revision": 1})

    def test_continuation_readback_reconciliation_uses_same_campaign(self):
        _, _, action = self.legacy_partial_creation()
        row = self.propose(action)
        with patch.object(self.products, "verify_ad", side_effect=RuntimeError("legacy readback failure")):
            result = self.approve_fake(row)
        self.assertEqual(result["state"], "uncertain")
        count = len(self.source.writes)
        recovered = self.products.reconcile_creation(row["id"])
        self.assertEqual(recovered["result"]["campaign_id"], action["campaign_id"])
        self.assertEqual(recovered["state"], "applied")
        self.assertEqual(len(self.source.writes), count)

    def test_creation_reconciliation_rejects_unknown_writes_and_missing_approval(self):
        with patch.object(self.products, "verify_ad", side_effect=RuntimeError("legacy readback failure")):
            _, row, _ = self.create()
        original = self.products.operation(row["id"])
        altered = copy.deepcopy(original)
        altered["steps"][-1]["status"] = "requested"
        self.store.set_setting("product_operation:" + row["id"], altered)
        with self.assertRaises(Conflict):
            self.products.reconcile_creation(row["id"])
        self.store.set_setting("product_operation:" + row["id"], original)
        with self.store.db() as db:
            db.execute("DELETE FROM events WHERE proposal_id=? AND event='approve'", (row["id"],))
        with self.assertRaises(Conflict):
            self.products.reconcile_creation(row["id"])
        self.assertEqual(self.store.get(row["id"])["state"], "uncertain")

    def test_creation_reconciliation_rejects_external_ad_and_group_changes(self):
        with patch.object(self.products, "verify_ad", side_effect=RuntimeError("legacy readback failure")):
            _, row, _ = self.create()
        original = copy.deepcopy(self.source.items["adgroups"][0])
        self.source.items["adgroups"][0]["RegionIds"] = [0]
        with self.assertRaises(Conflict):
            self.products.reconcile_creation(row["id"])
        self.source.items["adgroups"][0] = original
        self.source.items["ads"][0]["Status"] = "ACCEPTED"
        with self.assertRaises(Conflict):
            self.products.reconcile_creation(row["id"])

    def test_filter_readback_accepts_items_but_never_ignores_malformed_conditions(self):
        group = {"filters": [{"Operand": "categoryId", "Operator": "EQUALS_ANY", "Arguments": ["2"]}], "default_text": "Text"}
        for filters in (group["filters"], {"Items": group["filters"]}):
            self.products.verify_ad({"ShoppingAd": {"FeedId": 5, "DefaultTexts": ["Text"], "FeedFilterConditions": filters}}, 5, group)
        for filters in (None, {}, {"Items": None}, {"Items": ["categoryId"]}, [{"Operand": "categoryId"}],
                        {"Items": [{"Operand": "categoryId", "Operator": "EQUALS_ANY", "Arguments": ["999"]}]}):
            with self.assertRaises(RuntimeError):
                self.products.verify_ad({"ShoppingAd": {"FeedId": 5, "DefaultTexts": ["Text"], "FeedFilterConditions": filters}}, 5, group)

    def test_feed_registration_separate_and_no_campaign(self):
        self.source.items["feeds"] = []
        row = self.propose({"kind": "product_feed_register", "feed_url": URL})
        result = self.approve_fake(row)
        self.assertEqual(result["state"], "applied")
        self.assertEqual(self.source.items["campaigns"], [])
        saved = self.products.save_draft(draft())
        with self.assertRaises(Conflict):
            self.propose({"kind": "product_create", "draft_id": saved["id"], "draft_revision": 1})

    def test_feed_other_performance_schema_registration_and_creation_card(self):
        self.source.items["feeds"] = []
        self.source.feed_readback = {"BusinessType": "OTHER", "FilterSchema": "PerformanceDefault",
                                     "Status": "DONE", "NumberOfItems": 1}
        row = self.propose({"kind": "product_feed_register", "feed_url": URL})
        result = self.approve_fake(row)
        self.assertEqual(result["state"], "applied", result["result"])
        self.assertEqual(self.products.ready_feed(URL)["Id"], result["result"]["feed_id"])
        saved = self.products.save_draft(draft())
        self.propose({"kind": "product_create", "draft_id": saved["id"], "draft_revision": 1})
        self.assertEqual(len(self.source.writes), 1)
        self.assertEqual(self.source.items["campaigns"], [])

    def test_ready_feed_rejects_wrong_identity_schema_and_incomplete_processing(self):
        valid = self.source.items["feeds"][0] | {"BusinessType": "OTHER", "FilterSchema": "PerformanceDefault"}
        for change in ({"BusinessType": "HOTELS"}, {"FilterSchema": "SiteSchema"}, {"FilterSchema": None},
                       {"SourceType": "FILE"}, {"UrlFeed": {"Url": URL + "?other=1"}}, {"UrlFeed": None},
                       {"Status": "NEW"}, {"Status": "ERROR"}, {"NumberOfItems": None},
                       {"NumberOfItems": 0}, {"NumberOfItems": True}, {"NumberOfItems": "1"}):
            with self.subTest(change=change):
                self.source.items["feeds"] = [valid | change]
                with self.assertRaises(Conflict):
                    self.products.ready_feed(URL)

    def test_other_pending_feed_is_registered_but_not_ready(self):
        self.source.items["feeds"] = []
        self.source.feed_readback = {"BusinessType": "OTHER", "NumberOfItems": None}
        row = self.propose({"kind": "product_feed_register", "feed_url": URL})
        result = self.approve_fake(row)
        self.assertEqual(result["state"], "applied")
        self.assertEqual(result["result"]["processing_status"], "NEW")
        with self.assertRaises(Conflict):
            self.products.ready_feed(URL)
        self.assertEqual(len(self.source.writes), 1)

    def uncertain_registered_feed(self):
        self.source.items["feeds"] = []
        self.source.feed_readback = {"BusinessType": "OTHER", "FilterSchema": "PerformanceDefault",
                                     "Status": "DONE", "NumberOfItems": 1}
        row = self.propose({"kind": "product_feed_register", "feed_url": URL})
        # Reproduce the deployed legacy RETAIL-only readback failure after a successful add.
        with patch("marketer.products.native_feed_matches", return_value=False):
            result = self.approve_fake(row)
        self.assertEqual(result["state"], "uncertain")
        return row

    def test_reconcile_confirmed_feed_is_read_only_and_preserves_duplicate_guard(self):
        row = self.uncertain_registered_feed()
        result = self.products.reconcile_feed_registration(row["id"])
        self.assertEqual(result["state"], "applied")
        self.assertTrue(result["result"]["reconciled"])
        self.assertFalse(result["result"]["campaign_created"])
        self.assertEqual(len(self.source.writes), 1)
        with self.assertRaises(Conflict):
            self.products.reconcile_feed_registration(row["id"])
        with self.assertRaises(Conflict):
            self.propose({"kind": "product_feed_register", "feed_url": URL})
        with self.store.db() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM events WHERE proposal_id=? AND event='approve'",
                                        (row["id"],)).fetchone()[0], 1)
            self.assertEqual(db.execute("SELECT count(*) FROM events WHERE proposal_id=? AND event='feed_registration_reconciled'",
                                        (row["id"],)).fetchone()[0], 1)

    def test_reconcile_feed_requires_approval_and_exact_confirmed_id(self):
        row = self.uncertain_registered_feed()
        original = copy.deepcopy(self.source.items["feeds"][0])
        for change in ({"Id": 999}, {"UrlFeed": {"Url": URL + "?other=1"}}, {"Status": "ERROR"}, {"FilterSchema": "SiteSchema"}):
            with self.subTest(change=change):
                self.source.items["feeds"] = [original | change]
                with self.assertRaises(Conflict):
                    self.products.reconcile_feed_registration(row["id"])
                self.assertEqual(self.store.get(row["id"])["state"], "uncertain")
        self.source.items["feeds"] = [original]
        with self.store.db() as db:
            db.execute("DELETE FROM events WHERE proposal_id=? AND event='approve'", (row["id"],))
        with self.assertRaises(Conflict):
            self.products.reconcile_feed_registration(row["id"])
        self.assertEqual(self.store.get(row["id"])["state"], "uncertain")
        self.assertEqual(len(self.source.writes), 1)

    def test_reconcile_feed_rejects_unconfirmed_write(self):
        self.source.items["feeds"] = []
        row = self.propose({"kind": "product_feed_register", "feed_url": URL})
        self.source.fail = ("feeds", "add")
        self.approve_fake(row)
        with self.assertRaises(Conflict):
            self.products.reconcile_feed_registration(row["id"])
        self.assertEqual(len(self.source.writes), 1)

    def test_revised_draft_invalidates_previous_card(self):
        saved = self.products.save_draft(draft())
        row = self.propose({"kind": "product_create", "draft_id": saved["id"], "draft_revision": 1})
        self.products.save_draft(draft() | {"name": "Changed"}, saved["id"], 1)
        self.assertEqual(self.approve_fake(row)["state"], "stale")
        self.assertEqual(self.source.writes, [])

    def test_purchase_guard_all_channels(self):
        for placement in ("search_only", "network_only", "both"):
            strategy = self.source.build_unified_bidding_strategy(goal_id=PURCHASE_GOAL, cpa_micros=1000000000, weekly_budget_micros=20000000000, placement_type=placement)
            purchase_strategy({"Type": "UNIFIED_CAMPAIGN", "UnifiedCampaign": {"CounterIds": {"Items": [COUNTER_ID]}, "BiddingStrategy": strategy}})
        with self.assertRaises(YandexDirectClientError):
            self.source.build_unified_bidding_strategy(goal_id=352402525, cpa_micros=100, weekly_budget_micros=2000)
        valid, errors = validate_campaign({"metrica_goal_id": 352402525})
        self.assertTrue(any("purchase goal" in e for e in errors))

    def test_independent_lifecycle_and_edit(self):
        _, _, created = self.create()
        cid = created["result"]["campaign_id"]
        row = self.propose({"kind": "product_edit", "campaign_id": cid, "patch": {"name": "Reviewed pilot"}})
        self.assertEqual(self.approve_fake(row)["state"], "applied")
        aid = self.source.items["ads"][0]["Id"]
        row = self.propose({"kind": "product_ad_edit", "campaign_id": cid, "ad_id": aid, "patch": {"default_text": "Rare porcelain"}})
        self.assertEqual(self.approve_fake(row)["state"], "applied")
        with self.assertRaises(Conflict):
            self.propose({"kind": "product_launch", "campaign_id": cid})
        # Existing stopped campaigns remain supported; new OFF/DRAFT campaigns are gated separately.
        self.source.items["campaigns"][0]["State"] = "SUSPENDED"
        row = self.propose({"kind": "product_moderate", "campaign_id": cid})
        self.assertEqual(self.approve_fake(row)["state"], "applied")
        self.source.items["ads"][0]["Status"] = "ACCEPTED"
        row = self.propose({"kind": "product_launch", "campaign_id": cid})
        self.assertEqual(self.source.campaign(cid)["State"], "SUSPENDED")
        self.assertEqual(self.approve_fake(row)["state"], "applied")

    def test_external_change_blocks_write(self):
        _, _, created = self.create()
        cid = created["result"]["campaign_id"]
        row = self.propose({"kind": "product_edit", "campaign_id": cid, "patch": {"name": "New name"}})
        count = len(self.source.writes)
        self.source.items["campaigns"][0]["Name"] = "External"
        self.assertEqual(self.approve_fake(row)["state"], "stale")
        self.assertEqual(len(self.source.writes), count)

    def test_multi_action_patch_and_cart_goal_forbidden(self):
        with self.assertRaises(ValueError):
            self.products.save_draft(draft() | {"goal_id": 352402525})
        _, _, created = self.create()
        with self.assertRaises(ValueError):
            self.propose({"kind": "product_edit", "campaign_id": created["result"]["campaign_id"], "patch": {"name": "X", "target_cpa_rub": "1200"}})

    def test_old_campaign_cannot_pause_without_linked_active_replacement(self):
        _, _, created = self.create()
        cid = created["result"]["campaign_id"]
        with self.assertRaises(Conflict):
            self.propose({"kind": "product_pause_old", "campaign_id": cid, "replacement_campaign_id": cid})

    def test_feed_timeout_cannot_create_duplicate(self):
        self.source.items["feeds"] = []
        row = self.propose({"kind": "product_feed_register", "feed_url": URL})
        self.source.fail = ("feeds", "add")
        self.assertEqual(self.approve_fake(row)["state"], "uncertain")
        with self.assertRaises(Conflict):
            self.propose({"kind": "product_feed_register", "feed_url": URL})
        self.assertEqual(len(self.source.writes), 1)

    def test_catalog_change_invalidates_approval_without_writes(self):
        saved = self.products.save_draft(draft())
        row = self.propose({"kind": "product_create", "draft_id": saved["id"], "draft_revision": 1})
        self.products.fetcher = lambda url: parse_yml(xml().replace(b"15000.0", b"18000.0"), url)
        self.assertEqual(self.approve_fake(row)["state"], "stale")
        self.assertEqual(self.source.writes, [])

    def test_non_purchase_external_strategy_cannot_launch(self):
        _, _, created = self.create()
        cid = created["result"]["campaign_id"]
        self.source.items["ads"][0]["Status"] = "ACCEPTED"
        self.source.items["campaigns"][0]["UnifiedCampaign"]["BiddingStrategy"]["Search"]["PayForConversion"]["GoalId"] = 352402525
        with self.assertRaises(Conflict):
            self.propose({"kind": "product_launch", "campaign_id": cid})

    def test_product_creation_has_no_sales_evaluation(self):
        self.create()
        source, telegram = Mock(), Mock()
        runner = Runner(self.store, source, self.actions, telegram, {"conversion_lag_days": 7})
        runner.evaluations()
        source.report.assert_not_called()
        telegram.notify.assert_not_called()
        self.assertIsNotNone(self.store.list()[0]["evaluated_at"])

    def test_product_launch_evaluation_uses_purchase_not_missing_before(self):
        _, _, created = self.create()
        cid = created["result"]["campaign_id"]
        self.source.items["ads"][0]["Status"] = "ACCEPTED"
        self.source.items["campaigns"][0]["State"] = "SUSPENDED"
        row = self.propose({"kind": "product_launch", "campaign_id": cid})
        self.approve_fake(row)
        with self.store.db() as db:
            db.execute("UPDATE proposals SET applied_at=? WHERE id=?", (time.time() - 40 * 86400, row["id"]))
        self.source.report = Mock(return_value=[{"Clicks": 100, "Impressions": 2000, "Cost": 1000, "Conversions": 1, "conversions_by_goal": {str(PURCHASE_GOAL): 1}}])
        telegram = Mock()
        Runner(self.store, self.source, self.actions, telegram, {"conversion_lag_days": 7}).evaluations()
        self.assertEqual(self.source.report.call_args.args[5], PURCHASE_GOAL)
        self.assertIsNotNone(self.store.get(row["id"])["evaluated_at"])
        telegram.notify.assert_called_once()

    def test_unknown_purchase_never_means_zero(self):
        c = {"Id": 1, "Name": "Old", "current": {"Clicks": 1000, "Cost": 2000, "GoalTotals": {str(PURCHASE_GOAL): {"complete": False, "confirmed_sum": 0}}}}
        result = replacement_candidates({"created": time.time(), "campaigns": [c]})
        self.assertEqual(result["items"][0]["status"], "insufficient_data")
        c["current"]["GoalTotals"][str(PURCHASE_GOAL)] = {"complete": True, "confirmed_sum": 1}
        self.assertEqual(replacement_candidates({"created": time.time(), "campaigns": [c]})["items"][0]["status"], "preserve")


if __name__ == "__main__":
    unittest.main()
