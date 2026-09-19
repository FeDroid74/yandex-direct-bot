import copy
import json
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from marketer.actions import Actions
from marketer.data import DataSource, sufficient, totals
from marketer.store import Conflict, Store
from marketer.telegram import buttons, card
from services.search_terms_analyzer import analyze_search_terms, build_negative_preview_candidates


POLICY = json.loads((Path(__file__).parents[1]/"marketer/policy.json").read_text())


def snapshot():
    return {"run_id": "test", "created": time.time(), "current_from": "2026-08-01", "date_from": "2026-07-04", "date_to": "2026-08-28", "limits": [], "errors": [],
            "campaigns": [{"Id": 1, "Name": "Test", "goal_id": 7, "attribution": "AUTO", "strategy_goals": [7],
                           "UnifiedCampaign": {"BiddingStrategy": {"Search": {"BiddingStrategyType": "PAY_FOR_CONVERSION", "PayForConversion": {"GoalId": 7, "Cpa": 1000000, "WeeklySpendLimit": 10000000}}, "Network": {"BiddingStrategyType": "SERVING_OFF"}}},
                           "current": {"Clicks": 100, "Cost": 200, "Impressions": 1000, "Conversions": 10, "CPA": 20},
                           "previous": {}, "daily": [{"Date": "2026-08-20", "Clicks": 100, "Cost": 200, "Impressions": 1000, "Conversions": 10}],
                           "groups": [{"Id": 2, "CampaignId": 1, "NegativeKeywords": {"Items": ["old"]}}],
                           "ads": [{"Id": 3, "CampaignId": 1, "TextAd": {"Title": "Before", "Text": "Before text"}}],
                           "queries": [{"Query": "irrelevant phrase", "AdGroupId": "2", "Clicks": 30, "Cost": 200, "Conversions": 0, "conversions_by_goal": {"7": 0}}]}]}


def proposal(kind="add_negative"):
    action = {"kind": "add_negative", "ad_group_id": 2, "phrase": "irrelevant phrase"}
    if kind == "advisory":
        action = {"kind": "advisory", "task": "One manual experiment"}
    if kind == "ad_text":
        action = {"kind": "ad_text", "ad_id": 3, "fields": {"Title": "After"}}
    if kind == "strategy_value":
        action = {"kind": "strategy_value", "side": "Search", "field": "Cpa", "value_micros": 1100000}
    return {"campaign_id": 1, "area": "search", "title": "Test decision", "reason": "Observed facts", "expected_effect": "Hypothesis", "success_metric": "Goal CPA", "evaluate_after_days": 28, "action": action}


class FakeSource:
    def __init__(self):
        self.state = snapshot()["campaigns"][0]
        self.direct = self
        self.writes = 0
        self.fail = False

    def group(self, gid):
        return copy.deepcopy(self.state["groups"][0])

    def ad(self, aid):
        return copy.deepcopy(self.state["ads"][0])

    def campaign(self, cid):
        return copy.deepcopy(self.state)

    def report(self, *args):
        return copy.deepcopy(self.state["queries"])

    def update_ad_group_negative_keywords(self, gid, values):
        self.writes += 1
        self.state["groups"][0]["NegativeKeywords"]["Items"] = values
        if self.fail:
            raise TimeoutError("write outcome unknown")
        return {"result": {"UpdateResults": [{"Id": gid}]}}

    def update_text_ad_content(self, aid, **fields):
        self.writes += 1
        self.state["ads"][0]["TextAd"].update({{"title": "Title", "title2": "Title2", "text": "Text"}[k]: v for k, v in fields.items() if v is not None})
        return {"result": {"UpdateResults": [{"Id": aid}]}}

    def call_v501(self, service, method, params):
        self.writes += 1
        self.state["UnifiedCampaign"]["BiddingStrategy"] = params["Campaigns"][0]["UnifiedCampaign"]["BiddingStrategy"]
        return SimpleNamespace(body={"result": {"UpdateResults": [{"Id": 1}]}})


class ProposalTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=Path(__file__).parent, prefix="test-marketer-")
        self.store = Store(Path(self.tmp.name)/"test.sqlite")
        self.source = FakeSource()
        self.actions = Actions(self.source, self.store, POLICY)

    def tearDown(self):
        self.tmp.cleanup()

    def create(self, kind="add_negative"):
        body = self.actions.prepare(proposal(kind), snapshot())
        return self.store.create(body)[0]

    def test_creation_never_writes(self):
        self.create()
        self.assertEqual(self.source.writes, 0)

    def test_approval_is_idempotent_and_preserves_existing_negatives(self):
        row = self.create()
        for _ in range(2):
            result = self.actions.decide(row["id"], 1, "approve", "owner")
            self.assertEqual(result["state"], "applied")
        self.assertEqual(self.source.writes, 1)
        self.assertEqual(self.source.group(2)["NegativeKeywords"]["Items"], ["old", "irrelevant phrase"])

    def test_concurrent_clicks_write_once(self):
        row = self.create()
        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(lambda _: self.actions.decide(row["id"], 1, "approve", "owner"), range(4)))
        self.assertEqual(self.source.writes, 1)

    def test_edit_requires_new_approval(self):
        row = self.create()
        body = copy.deepcopy(row["body"])
        body["reason"] = "Revised"
        new = self.store.revise(row["id"], 1, body)
        self.assertEqual(new["revision"], 2)
        with self.assertRaises(Conflict):
            self.actions.decide(row["id"], 1, "approve", "owner")
        self.assertEqual(self.source.writes, 0)

    def test_independent_rejection(self):
        a = self.create()
        b = self.create("ad_text")
        self.actions.decide(b["id"], 1, "reject", "owner")
        self.actions.decide(a["id"], 1, "approve", "owner")
        self.assertEqual(self.store.get(b["id"])["state"], "rejected")
        self.assertEqual(self.source.writes, 1)

    def test_no_duplicate_after_rejection(self):
        row = self.create()
        self.actions.decide(row["id"], 1, "reject", "owner")
        same, created = self.store.create(row["body"])
        self.assertFalse(created)
        self.assertEqual(same["id"], row["id"])

    def test_ambiguous_write_never_retried(self):
        row = self.create()
        self.source.fail = True
        for _ in range(2):
            result = self.actions.decide(row["id"], 1, "approve", "owner")
            self.assertEqual(result["state"], "uncertain")
        self.assertEqual(self.source.writes, 1)

    def test_changed_field_blocks_only_affected_card(self):
        row = self.create("ad_text")
        self.source.state["ads"][0]["TextAd"]["Title"] = "External edit"
        result = self.actions.decide(row["id"], 1, "approve", "owner")
        self.assertEqual(result["state"], "stale")
        self.assertEqual(self.source.writes, 0)

    def test_late_conversion_blocks_negative(self):
        row = self.create()
        self.source.state["queries"][0]["Conversions"] = 1
        result = self.actions.decide(row["id"], 1, "approve", "owner")
        self.assertEqual(result["state"], "stale")
        self.assertEqual(self.source.writes, 0)

    def test_multiplegoal_conversion_blocks_negative(self):
        data = snapshot()
        data["campaigns"][0]["queries"][0]["conversions_by_goal"]["8"] = 1
        with self.assertRaises(ValueError):
            self.actions.prepare(proposal(), data)

    def test_phrase_cannot_be_broadened(self):
        raw = proposal()
        raw["action"]["phrase"] = "phrase"
        with self.assertRaises(ValueError):
            self.actions.prepare(raw, snapshot())

    def test_small_sample_blocks_negative(self):
        data = snapshot()
        data["campaigns"][0]["queries"][0]["Clicks"] = 2
        with self.assertRaises(ValueError):
            self.actions.prepare(proposal(), data)

    def test_manual_plan_is_not_falsely_applied(self):
        row = self.create("advisory")
        result = self.actions.decide(row["id"], 1, "approve", "owner")
        self.assertEqual(result["state"], "accepted_manual")
        self.assertEqual(self.source.writes, 0)

    def test_ad_text_write(self):
        row = self.create("ad_text")
        result = self.actions.decide(row["id"], 1, "approve", "owner")
        self.assertEqual(result["state"], "applied")
        self.assertEqual(self.source.ad(3)["TextAd"]["Text"], "Before text")

    def test_strategy_preserves_other_channel(self):
        row = self.create("strategy_value")
        result = self.actions.decide(row["id"], 1, "approve", "owner")
        self.assertEqual(result["state"], "applied")
        self.assertEqual(self.source.campaign(1)["UnifiedCampaign"]["BiddingStrategy"]["Network"], {"BiddingStrategyType": "SERVING_OFF"})

    def test_strategy_large_change_blocked(self):
        raw = proposal("strategy_value")
        raw["action"]["value_micros"] = 9000000
        with self.assertRaises(ValueError):
            self.actions.prepare(raw, snapshot())

    def test_recovery_never_replays_inflight(self):
        row = self.create()
        self.store.decide(row["id"], 1, "approve", "owner")
        self.store.recover()
        self.assertEqual(self.store.get(row["id"])["state"], "uncertain")

    def test_expired_approval(self):
        row = self.create()
        with self.store.db() as db:
            db.execute("UPDATE proposals SET expires=0 WHERE id=?", (row["id"],))
        result = self.actions.decide(row["id"], 1, "approve", "owner")
        self.assertEqual(result["state"], "expired")
        self.assertEqual(self.source.writes, 0)

    def test_callback_and_card_limits(self):
        row = self.create()
        self.assertLessEqual(len(card(row)), 4096)
        for line in buttons(row):
            for button in line:
                self.assertLessEqual(len(button["callback_data"].encode()), 64)


class AnalyticsTests(unittest.TestCase):
    def test_converting_high_spend_is_never_negative(self):
        rows = [{"Query": "artfarfor фото", "Clicks": 50, "Cost": 3000, "Conversions": 10}]
        analysis = analyze_search_terms(rows, 500)
        self.assertEqual(analysis["candidates_negative"], [])
        self.assertEqual(build_negative_preview_candidates(rows, analysis, 500), [])

    def test_unknown_conversion_is_not_zero(self):
        for value in [None, "--", "", "bad", float("nan")]:
            rows = [{"Query": "free", "Clicks": 100, "Cost": 3000, "Conversions": value}]
            self.assertEqual(analyze_search_terms(rows, 500)["candidates_negative"], [])

    def test_no_token_broadening(self):
        rows = [{"Query": "скачать рисунок", "Clicks": 30, "Cost": 300, "Conversions": 0}]
        items = build_negative_preview_candidates(rows, analyze_search_terms(rows))
        self.assertEqual(items[0]["suggested_negative"], "скачать рисунок")

    def test_empty_data_skips_model(self):
        data = snapshot()
        data["campaigns"] = []
        self.assertFalse(sufficient(data, None, POLICY)[0])

    def test_identical_snapshot_skips_model(self):
        data = snapshot()
        self.assertFalse(sufficient(data, copy.deepcopy(data), POLICY)[0])

    def test_first_sufficient_snapshot_passes(self):
        self.assertTrue(sufficient(snapshot(), None, POLICY)[0])

    def test_pagination(self):
        direct = SimpleNamespace(call_v501=lambda _s, _m, p: SimpleNamespace(body={"result": {"Ads": [{"Id": p["Page"]["Offset"]}], **({"LimitedBy": 1} if p["Page"]["Offset"] == 0 else {})}}))
        source = DataSource(POLICY, direct=direct)
        self.assertEqual(len(source.entities("ads", "Ads", {})), 2)

    def test_api_top_level_error_not_success(self):
        direct = SimpleNamespace(call_v501=lambda *a: SimpleNamespace(body={"error": {"error_code": 53}}))
        with self.assertRaises(Exception):
            DataSource(POLICY, direct=direct).entities("ads", "Ads", {})

    def test_unknown_conversions_are_exposed_in_totals(self):
        result = totals([{"Clicks": 20, "Cost": 50, "Impressions": 100, "Conversions": None,
                          "conversions_by_goal": {"7": None, "8": 2}}])
        self.assertFalse(result["ConversionsComplete"])
        self.assertIsNone(result["CPA"])
        self.assertEqual(result["GoalTotals"]["8"]["confirmed_sum"], 2)

    def test_metrika_sampling_is_not_accepted(self):
        client = SimpleNamespace(_get_json=lambda *a, **k: {"sampled": True, "data": [], "total_rows": 0})
        source = DataSource(POLICY, metrika=client)
        with self.assertRaises(ValueError):
            source.metrika_report(1, "2026-08-01", "2026-08-28", 7, "ym:s:startURLPath")

    def test_invalid_tsv_is_not_an_empty_success(self):
        direct = SimpleNamespace(_build_reports_url=lambda: "https://api.direct.yandex.com/json/v5/reports", _build_reports_headers=lambda: {})
        class Response:
            status = 200
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def read(self, *args): return b'{"error":{"error_code":53}}'
        with patch("marketer.data.urllib.request.urlopen", return_value=Response()):
            with self.assertRaises(ValueError):
                DataSource(POLICY, direct=direct).report(1, "2026-08-01", "2026-08-28", "CAMPAIGN_PERFORMANCE_REPORT", ["Date", "CampaignId"], 7, "AUTO")

    def test_stale_snapshot_cannot_generate_new_card(self):
        data = snapshot()
        data["created"] = 0
        with self.assertRaises(ValueError):
            Actions(FakeSource(), None, POLICY).prepare(proposal(), data)


if __name__ == "__main__":
    unittest.main()
