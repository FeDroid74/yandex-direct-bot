import copy
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from marketer.actions import Actions
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

    def entities(self, service, key, params):
        criteria = params.get("SelectionCriteria", {})
        values = self.items[service]
        if "CampaignIds" in criteria:
            values = [v for v in values if v["CampaignId"] in criteria["CampaignIds"]]
        return copy.deepcopy(values)

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
        self.assertEqual(self.source.campaign(cid)["State"], "SUSPENDED")
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
