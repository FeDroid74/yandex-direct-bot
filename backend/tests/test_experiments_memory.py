import copy
import json
import tempfile
import time
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import Mock, patch
from zoneinfo import ZoneInfo

from marketer.actions import Actions
from marketer.assortment import record_catalog, review, baseline_time
from marketer.catalog import assortment_summary
from marketer.experiments import Experiments, windows
from marketer.memory import read_memory, read_experiment, memory_context
from marketer.products import Products
from marketer.runner import Runner
from marketer.store import Store
from test_marketer import FakeSource, POLICY, proposal, snapshot


def feed(at, price="100", available=True):
    return {"url": "https://artfarfor.com/products.xml", "sha256": "x", "fetched_at": at,
            "generated_at": datetime.fromtimestamp(at, ZoneInfo("Europe/Moscow")).isoformat(),
            "categories": {"1": {"name": "Porcelain", "parent_id": None}}, "invalid_count": 0,
            "total": 1, "available": int(available), "unavailable": int(not available), "errors": [],
            "offers": [{"id": "10", "name": "Figure", "url": "https://artfarfor.com/product/figure", "price": price,
                        "category_id": "1", "available": available, "pictures": ["https://artfarfor.com/image.jpg"]}]}


class MemoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="experiment-tests-")
        self.store = Store(Path(self.tmp.name)/"test.sqlite3")
        self.source = FakeSource()
        self.policy = {**POLICY, "goal_id": 7}
        self.actions = Actions(self.source, self.store, self.policy)
        self.telegram = Mock()
        self.engine = Experiments(self.store, self.source, self.telegram, self.policy)
        self.now = time.time()

    def tearDown(self):
        self.tmp.cleanup()

    def make(self, kind="ad_text", days=50):
        body = self.actions.prepare(proposal(kind), snapshot())
        row = self.store.create(body)[0]
        self.store.finish(row["id"], "applied", {})
        with self.store.db() as db:
            db.execute("UPDATE proposals SET applied_at=? WHERE id=?", (self.now-days*86400, row["id"]))
        if kind == "ad_text":
            self.source.state["ads"][0]["TextAd"]["Title"] = "After"
        if kind == "add_negative":
            self.source.state["groups"][0]["NegativeKeywords"]["Items"].append("irrelevant phrase")
        self.source.report = Mock(return_value=[{"Clicks": 150, "Impressions": 1000, "Cost": 1000, "Conversions": 12,
                                               "conversions_by_goal": {"7": 12, "8": 25}}])
        return self.store.get(row["id"])

    def test_equal_mature_periods_exclude_application_day(self):
        row = self.make()
        w = windows(row, self.policy, self.now)
        self.assertEqual((datetime.fromisoformat(w["before_to"])-datetime.fromisoformat(w["before_from"])).days+1, w["days"])
        self.assertEqual((datetime.fromisoformat(w["date_to"])-datetime.fromisoformat(w["date_from"])).days+1, w["days"])
        self.assertEqual((datetime.fromisoformat(w["date_from"])-datetime.fromisoformat(w["before_to"])).days, 2)
        mature = datetime.fromtimestamp(self.now, ZoneInfo("Europe/Moscow")).date()-timedelta(days=8)
        self.assertLessEqual(w["date_to"], mature.isoformat())

    def test_not_due_does_not_query_or_notify(self):
        self.make(days=20)
        self.engine.run(self.now)
        self.source.report.assert_not_called()
        self.telegram.notify.assert_not_called()

    def test_observation_uses_primary_goal_and_separate_goals(self):
        row = self.make()
        self.engine.run(self.now)
        data = self.store.observation(row["id"])["data"]
        self.assertEqual(data["status"], "observed")
        self.assertEqual(data["after"]["Conversions"], 12)
        self.assertEqual(data["after"]["GoalTotals"]["8"]["confirmed_sum"], 25)
        self.assertFalse(data["causal_effect_proven"])
        self.assertFalse(data["paid_orders_available"])
        self.assertEqual(self.source.report.call_count, 2)
        for call in self.source.report.call_args_list:
            self.assertEqual(call.args[3:5], ("CAMPAIGN_PERFORMANCE_REPORT", ["CampaignId"]))
        self.assertEqual(self.source.writes, 0)

    def test_unknown_goal_is_not_zero_or_failure(self):
        row = self.make()
        self.source.report.return_value[0]["Conversions"] = None
        self.source.report.return_value[0]["conversions_by_goal"]["7"] = None
        self.engine.run(self.now)
        data = self.store.observation(row["id"])["data"]
        self.assertEqual(data["status"], "insufficient_data")
        self.assertIsNone(data["after"]["CPA"])
        self.assertIsNone(self.store.get(row["id"])["evaluated_at"])

    def test_shopping_copy_is_measured_and_feed_drift_is_a_confounder(self):
        row = self.make()
        row["body"]["action"] = {"kind": "product_ad_edit", "campaign_id": row["body"]["campaign_id"],
                                 "ad_id": 999, "patch": {"default_text": "After"}}
        original = {"Id": 999, "ShoppingAd": {"FeedId": 11, "FeedFilterConditions": [{"Category": 1}]}}
        row["body"]["before"] = {"ads": [original]}
        self.source.ad = Mock(return_value={"ShoppingAd": {**original["ShoppingAd"], "DefaultTexts": ["After"]}})
        with self.store.db() as db:
            db.execute("UPDATE proposals SET body=? WHERE id=?", (json.dumps(row["body"]), row["id"]))
        self.engine.run(self.now)
        data = self.store.observation(row["id"])["data"]
        self.assertEqual(data["status"], "observed")
        self.assertEqual(self.source.report.call_count, 2)
        self.source.ad.return_value["ShoppingAd"]["FeedId"] = 12
        result, _ = self.engine.assess(row, windows(row, self.policy, self.now))
        self.assertEqual(result["status"], "confounded")
        self.assertIn("shopping_feed_or_selection_changed", result["confounders"])

    def test_small_sample_ends_as_inconclusive_after_90_mature_days(self):
        row = self.make(days=110)
        self.source.report.return_value[0]["Conversions"] = 0
        self.engine.run(self.now)
        data = self.store.observation(row["id"])["data"]
        self.assertEqual(data["status"], "inconclusive")
        self.assertEqual(data["days"], 90)
        self.assertIsNotNone(self.store.get(row["id"])["evaluated_at"])

    def test_other_changes_block_causal_interpretation(self):
        row = self.make()
        other = self.store.create(self.actions.prepare(proposal("advisory"), snapshot()))[0]
        with self.store.db() as db:
            db.execute("INSERT INTO events(proposal_id,revision,event,actor,at,data) VALUES(?,1,'done','owner',?,?)",
                       (other["id"], self.now-20*86400, json.dumps({"state": "completed_manual"})))
        self.engine.run(self.now)
        data = self.store.observation(row["id"])["data"]
        self.assertEqual(data["status"], "confounded")
        self.assertIn("other_recorded_campaign_changes", data["confounders"])

    def test_external_strategy_change_is_flagged(self):
        row = self.make()
        self.source.state["UnifiedCampaign"]["CounterIds"] = {"Items": [999]}
        self.engine.run(self.now)
        self.assertIn("campaign_configuration_changed", self.store.observation(row["id"])["data"]["confounders"])

    def test_attribution_change_blocks_comparison(self):
        row = self.make()
        self.source.state["UnifiedCampaign"]["AttributionModel"] = "FIRST_CLICK"
        self.engine.run(self.now)
        self.assertEqual(self.store.observation(row["id"])["data"]["status"], "not_comparable")
        self.source.report.assert_not_called()

    def test_read_failure_is_saved_and_does_not_block_other_runs(self):
        row = self.make()
        self.source.report.side_effect = TimeoutError("timeout")
        self.engine.run(self.now)
        self.assertEqual(self.store.observation(row["id"])["data"]["status"], "data_unavailable")
        self.assertIsNone(self.store.get(row["id"])["evaluated_at"])

    def test_no_extra_read_or_notice_within_week(self):
        self.make()
        self.source.report.return_value[0]["Conversions"] = 1
        self.engine.run(self.now)
        self.engine.run(self.now+3600)
        self.assertEqual(self.source.report.call_count, 2)
        self.telegram.notify.assert_called_once()

    def test_unknown_delivery_not_replayed(self):
        row = self.make()
        self.telegram.notify.side_effect = TimeoutError("unknown")
        self.engine.run(self.now)
        self.engine.run(self.now+3600)
        self.telegram.notify.assert_called_once()
        self.assertEqual(self.store.observation(row["id"])["notification"], "uncertain")

    def test_same_status_new_data_does_not_spam(self):
        row = self.make()
        self.source.report.return_value[0]["Conversions"] = 1
        self.engine.run(self.now)
        with self.store.db() as db:
            db.execute("UPDATE experiment_observations SET updated=?", (self.now-8*86400,))
        self.engine.run(self.now+7*86400)
        self.telegram.notify.assert_called_once()
        self.assertGreater(self.store.observation(row["id"])["data"]["days"], 40)

    def test_memory_preserves_owner_states_and_outcome(self):
        row = self.make()
        self.engine.run(self.now)
        data = read_memory(self.store, 1)
        self.assertEqual(data["items"][0]["evaluation"]["status"], "observed")
        self.assertEqual(data["items"][0]["evaluation"]["after"]["Conversions"], 12)
        self.assertEqual(read_experiment(self.store, row["id"])["decision"]["state"], "applied")

    def test_manual_completion_is_not_an_experiment_success(self):
        row = self.store.create(self.actions.prepare(proposal("advisory"), snapshot()))[0]
        self.actions.decide(row["id"], 1, "approve", "owner")
        self.actions.decide(row["id"], 1, "done", "owner")
        self.engine.run(self.now)
        result = read_memory(self.store, 1)["items"][0]
        self.assertEqual(result["state"], "completed_manual")
        self.assertIsNone(result["evaluation"])
        self.assertTrue(result["manual_completion_is_not_proof_of_effect"])
        self.assertEqual(self.source.writes, 0)

    def test_old_active_proposal_not_duplicated(self):
        body = self.actions.prepare(proposal("advisory"), snapshot())
        row = self.store.create(body)[0]
        with self.store.db() as db:
            db.execute("UPDATE proposals SET updated=? WHERE id=?", (self.now-100*86400, row["id"]))
        same, fresh = self.store.create(body)
        self.assertFalse(fresh)
        self.assertEqual(same["id"], row["id"])

    def test_memory_pagination_and_campaign_isolation(self):
        for i in range(13):
            body = self.actions.prepare(proposal("advisory"), snapshot())
            body["action"]["task"] += str(i)
            self.store.create(body)
        self.assertEqual(read_memory(self.store, 1)["next_page"], 2)
        self.assertEqual(len(read_memory(self.store, 1, 2)["items"]), 3)
        self.assertEqual(read_memory(self.store, 2)["total"], 0)
        self.assertTrue(memory_context(self.store, [1])["sampled"])
        self.assertEqual(len(memory_context(self.store, [1])["items"]), 3)

    def test_shared_measurement_tasks_visible_across_batches(self):
        body = self.actions.prepare(proposal("advisory"), snapshot())
        body["area"] = "measurement"
        row = self.store.create(body)[0]
        context = memory_context(self.store, [2])
        self.assertEqual(context["items"], [])
        self.assertEqual(context["shared_measurement_tasks"][0]["id"], row["id"])

    def test_latest_outcome_not_hidden_by_new_pending_tasks(self):
        row = self.make()
        self.engine.run(self.now)
        for i in range(5):
            body = self.actions.prepare(proposal("advisory"), snapshot())
            body["action"]["task"] += str(i)
            self.store.create(body)
        items = memory_context(self.store, [1])["items"]
        self.assertIn(row["id"], [r["id"] for r in items])
        self.assertTrue(any(r["state"] == "pending" for r in items))

    def test_revision_and_legacy_evaluation_remain_readable(self):
        body = self.actions.prepare(proposal("advisory"), snapshot())
        row = self.store.create(body)[0]
        changed = copy.deepcopy(body)
        changed["action"]["task"] = "Revised manual task"
        self.store.revise(row["id"], 1, changed)
        self.store.evaluate(row["id"], {"conclusion": "Legacy observation, not causal"})
        value = read_experiment(self.store, row["id"])
        self.assertEqual(value["decision"]["evaluation"]["conclusion"], "Legacy observation, not causal")
        original = next(e for e in value["events"] if e["event"] == "created")
        self.assertEqual(original["data"]["action"], body["action"])

    def test_completed_experiment_not_requeried(self):
        self.make()
        self.engine.run(self.now)
        self.engine.run(self.now+8*86400)
        self.assertEqual(self.source.report.call_count, 2)
        self.telegram.notify.assert_called_once()

    def test_feed_history_has_no_sales_inference(self):
        first = feed(self.now-10*86400)
        second = feed(self.now, price="150", available=False)
        for value in (first, second):
            record_catalog(self.store, value)
            self.store.set_setting("product_catalog_last", assortment_summary(value))
        result = review(self.store, baseline_at=self.now-9*86400, now=self.now)
        self.assertEqual(result["changes"]["price_changed"]["count"], 1)
        self.assertEqual(result["changes"]["availability_changed"]["count"], 1)
        self.assertNotIn("sales", result)

    def test_baseline_is_feed_observation_not_earlier_report_collection(self):
        data = {"created": 100, "assortment": {"catalog": {"fetched_at": 400}}}
        self.assertEqual(baseline_time(data), 400)
        self.assertEqual(baseline_time({"created": 100}), 100)

    def test_feed_same_contents_no_extra_snapshot(self):
        record_catalog(self.store, feed(self.now-100))
        record_catalog(self.store, feed(self.now))
        with self.store.db() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM catalog_snapshots").fetchone()[0], 1)

    def test_invalid_feed_does_not_claim_disappearance(self):
        record_catalog(self.store, feed(self.now-100))
        invalid = feed(self.now)
        invalid["offers"] = []
        invalid["invalid_count"] = 1
        record_catalog(self.store, invalid)
        self.store.set_setting("product_catalog_last", assortment_summary(invalid))
        result = review(self.store, baseline_at=self.now-50)
        self.assertIsNone(result["changes"])

    def test_stale_and_failed_feed_not_current(self):
        value = feed(self.now-3*86400)
        record_catalog(self.store, value)
        self.store.set_setting("product_catalog_last", assortment_summary(value))
        self.assertEqual(review(self.store)["status"], "stale")
        self.store.set_setting("product_catalog_last", {"status": "unavailable", "url": value["url"]})
        self.assertEqual(review(self.store)["status"], "unavailable")

    def test_recent_fetch_does_not_make_old_generation_fresh(self):
        value = feed(self.now)
        value["generated_at"] = datetime.fromtimestamp(self.now-3*86400, ZoneInfo("Europe/Moscow")).isoformat()
        record_catalog(self.store, value)
        self.store.set_setting("product_catalog_last", assortment_summary(value))
        self.assertEqual(review(self.store)["status"], "stale")

    def test_product_mapping_uses_live_filters_not_names(self):
        value = feed(self.now)
        Products(self.source, self.store, fetcher=lambda url:value).catalog(value["url"])
        self.store.set_setting("product_campaign:1", {"feed_id": 9, "plan": {"feed_url": value["url"]}})
        c = {"Id": 1, "ads": [{"Id": 5, "ShoppingAd": {"FeedId": 9, "FeedFilterConditions": {"Items": [
            {"Operand": "categoryId", "Operator": "EQUALS_ANY", "Arguments": ["1"]}]}}}]}
        result = review(self.store, [c])
        self.assertEqual(result["campaign_selections"][0]["available"], 1)
        c["ads"][0]["ShoppingAd"]["FeedFilterConditions"]["Items"][0]["Operator"] = "UNKNOWN"
        self.assertEqual(review(self.store, [c])["campaign_selections"][0]["status"], "unknown_filter")


if __name__ == "__main__":
    unittest.main()
