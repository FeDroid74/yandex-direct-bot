import copy
import json
import re
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from marketer.actions import Actions
from marketer.data import DataSource, sufficient, totals
from marketer.diagnostics import campaign_diagnostics
from marketer.runner import AREAS, Runner, sample_rows
from marketer.schedule import weekly_slot
from marketer.store import Store
from marketer.telegram import card, detail_pages
from services.metrika_client import YandexMetrikaClientError
from test_marketer import FakeSource, POLICY, proposal, snapshot


def timestamp(value):
    return datetime.fromisoformat(value).replace(tzinfo=timezone.utc).timestamp()


def review():
    return {"summary": "Checked", "proposals": [],
            "coverage": {area: {"status": "no_action", "reason": "No justified action"} for area in AREAS}}


class ScheduleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=Path(__file__).parent, prefix="test-marketer-")
        self.store = Store(Path(self.tmp.name)/"test.sqlite")
        self.source = Mock()
        self.source.collect.return_value = snapshot()
        self.runner = Runner(self.store, self.source, Mock(), Mock(), POLICY)
        self.runner.model = Mock(return_value=review())

    def tearDown(self):
        self.tmp.cleanup()

    def test_calendar_boundary_and_dst(self):
        self.assertEqual(weekly_slot(timestamp("2026-09-21T09:59:59")), "2026-09-14")
        self.assertEqual(weekly_slot(timestamp("2026-09-21T10:00:00")), "2026-09-21")
        self.assertEqual(weekly_slot(timestamp("2026-10-26T10:59:59")), "2026-10-19")
        self.assertEqual(weekly_slot(timestamp("2026-10-26T11:00:00")), "2026-10-26")

    def test_september_21_runs_after_manual_september_19_once(self):
        self.store.set_setting("last_attempt", timestamp("2026-09-19T13:20:00"))
        self.store.set_setting("last_analyzed_snapshot", snapshot())
        with patch("marketer.runner.time.time", return_value=timestamp("2026-09-21T10:00:01")):
            self.assertEqual(self.runner.run(scheduled=True)["status"], "complete")
            self.assertEqual(self.runner.run(scheduled=True)["status"], "already_scheduled")
        self.runner.model.assert_called_once()
        self.source.collect.assert_called_once()
        self.assertEqual(self.store.runs()[0]["data"]["snapshot"]["schedule_slot"], "2026-09-21")

    def test_normal_monday_collects_but_identical_data_does_not_call_model(self):
        self.store.set_setting("last_attempt", timestamp("2026-09-27T13:20:00"))
        self.store.set_setting("last_analyzed_snapshot", snapshot())
        with patch("marketer.runner.time.time", return_value=timestamp("2026-09-28T10:00:01")):
            self.assertEqual(self.runner.run(scheduled=True)["status"], "skipped")
        self.source.collect.assert_called_once()
        self.runner.model.assert_not_called()

    def test_required_date_does_not_analyze_empty_data(self):
        self.source.collect.return_value["campaigns"] = []
        with patch("marketer.runner.time.time", return_value=timestamp("2026-09-21T10:00:01")):
            self.assertEqual(self.runner.run(scheduled=True)["status"], "skipped")
        self.runner.model.assert_not_called()

    def test_scheduled_failure_is_not_blindly_replayed(self):
        self.source.collect.side_effect = RuntimeError("read failed")
        with patch("marketer.runner.time.time", return_value=timestamp("2026-09-21T10:00:01")):
            with self.assertRaises(RuntimeError):
                self.runner.run(scheduled=True)
            self.assertEqual(self.runner.run(scheduled=True)["status"], "already_scheduled")
        self.source.collect.assert_called_once()

    def test_manual_cooldown_and_invalid_scheduled_flags(self):
        self.store.set_setting("last_attempt", timestamp("2026-09-19T10:00:00"))
        with patch("marketer.runner.time.time", return_value=timestamp("2026-09-19T11:00:00")):
            self.assertEqual(self.runner.run()["status"], "not_due")
            for args in ({"force": True}, {"collect_only": True}):
                with self.assertRaises(ValueError):
                    self.runner.run(scheduled=True, **args)
        self.source.collect.assert_not_called()


class FakeMetrika:
    def __init__(self):
        self.requests = []

    def get_counter(self, cid):
        return {"counter": {"id": cid, "status": "Active", "code_status": "CS_ERR_UNKNOWN", "site": "example.com", "time_zone_name": "Europe/Moscow"}}

    def get_goals(self, cid):
        return {"goals": [{"id": 7, "name": "Purchase", "type": "e_purchase", "status": "Active"},
                          {"id": 8, "name": "Cart", "type": "e_cart", "status": "Active"}]}

    def _get_json(self, path, params):
        self.requests.append(params)
        base = {"ym:s:visits": 100, "ym:s:users": 90, "ym:s:bounceRate": 20, "ym:s:pageDepth": 1.5, "ym:s:avgVisitDurationSeconds": 40}
        values = []
        for metric in params["metrics"].split(","):
            if metric in base:
                values.append(base[metric])
            else:
                match = re.fullmatch(r"ym:s:goal(\d+)(reaches|visits)", metric)
                values.append(0 if match[1] == "7" else (13 if match[2] == "reaches" else 12))
        return {"total_rows": 1, "sampled": False, "totals": values,
                "data": [{"dimensions": [{"name": "/catalog"}] if params.get("dimensions") else [], "metrics": values}]}


class CollectedSource(DataSource):
    def __init__(self):
        self.policy = {**POLICY, "counter_id": 99, "goal_id": 7}
        self.metrika = FakeMetrika()
        self.report_calls = []

    def entities(self, service, key, params):
        c = snapshot()["campaigns"][0]
        return [{"Id": 1}] if service == "campaigns" else c["groups" if service == "adgroups" else "ads"]

    def campaign(self, cid):
        c = snapshot()["campaigns"][0]
        c["UnifiedCampaign"]["CounterIds"] = {"Items": [99]}
        c["UnifiedCampaign"]["PriorityGoals"] = {"Items": [{"GoalId": 7}, {"GoalId": 8}]}
        return c

    def report(self, cid, start, end, kind, dimensions, goal_id, attribution, extra_goals=None):
        self.report_calls.append((kind, dimensions, set(extra_goals or [])))
        row = {"CampaignId": "1", "Clicks": 100, "Impressions": 1000, "Cost": 200, "Conversions": None,
               "conversions_by_goal": {"7": None, "8": 5}, "raw_conversions_by_goal": {"7": "--", "8": "5"}}
        for dimension in dimensions:
            row.setdefault(dimension, {"Date": "2026-08-20", "Query": "test query", "AdGroupId": "2", "AdId": "3"}.get(dimension, "test"))
        return [row]


class MultiGoalTests(unittest.TestCase):
    def test_metrika_visits_and_reaches_are_separate(self):
        source = DataSource(POLICY, direct=Mock(), metrika=FakeMetrika())
        result = source.metrika_report(1, "2026-08-01", "2026-08-28", 7, extra_goals=[8])
        self.assertEqual(result["totals"]["goals"], {"7": {"reaches": 0, "visits": 0}, "8": {"reaches": 13, "visits": 12}})
        self.assertEqual(result["totals"]["goal_reaches"], 0)
        self.assertIn("filters", source.metrika.requests[0])
        source.metrika_report(None, "2026-08-01", "2026-08-28", 7)
        self.assertNotIn("filters", source.metrika.requests[-1])

    def test_metrika_many_goals_are_batched_below_metric_limit(self):
        source = DataSource(POLICY, direct=Mock(), metrika=FakeMetrika())
        result = source.metrika_report(1, "2026-08-01", "2026-08-28", 1, extra_goals=list(range(2, 11)))
        self.assertEqual(set(result["totals"]["goals"]), {str(g) for g in range(1, 11)})
        self.assertEqual(len(source.metrika.requests), 2)
        self.assertTrue(all(len(p["metrics"].split(",")) <= 20 for p in source.metrika.requests))

    def test_period_aggregate_does_not_inherit_unknown_daily_rows(self):
        daily = [{"Clicks": 10, "Cost": 100, "Impressions": 100, "Conversions": None, "conversions_by_goal": {"7": None, "8": None}},
                 {"Clicks": 10, "Cost": 100, "Impressions": 100, "Conversions": 1, "conversions_by_goal": {"7": 1, "8": 5}}]
        aggregate = [{"Clicks": 20, "Cost": 200, "Impressions": 200, "Conversions": 1, "conversions_by_goal": {"7": 1, "8": 5}}]
        self.assertIsNone(totals(daily)["CPA"])
        result = totals(aggregate)
        self.assertEqual(result["CPA"], 200)
        self.assertEqual(result["GoalTotals"]["8"]["cpa"], 40)
        self.assertEqual(result["Conversions"], 1)
        self.assertFalse(totals([], [7])["GoalTotals"]["7"]["complete"])

    def test_collection_uses_aggregate_periods_and_all_strategy_goals(self):
        source = CollectedSource()
        result = source.collect(datetime(2026, 9, 5).date())
        self.assertEqual(result["errors"], [])
        c = result["campaigns"][0]
        self.assertEqual(result["schema_version"], 2)
        self.assertEqual(c["current"]["source"], "direct_period_report")
        self.assertEqual(c["current"]["GoalTotals"]["8"]["confirmed_sum"], 5)
        self.assertTrue(c["current"]["GoalTotals"]["8"]["complete"])
        self.assertEqual(set(c["metrika_periods"]), {"current", "previous"})
        self.assertTrue(all(goals == {7, 8} for _, _, goals in source.report_calls))
        self.assertTrue(c["diagnostics"]["counter_linked"])
        self.assertEqual(c["diagnostics"]["goals"]["7"]["counter_observation"], "not_observed_in_period")
        self.assertEqual(c["diagnostics"]["goals"]["8"]["counter_observation"], "observed_in_period")
        self.assertFalse(c["diagnostics"]["tracking_failure_proven"])

    def test_metadata_failure_does_not_stop_independent_analysis(self):
        source = CollectedSource()
        source.metrika.get_goals = Mock(side_effect=RuntimeError("metadata unavailable"))
        result = source.collect(datetime(2026, 9, 5).date())
        self.assertEqual(len(result["campaigns"]), 1)
        self.assertFalse(result["measurement"]["catalog_complete"])
        self.assertIsNone(result["campaigns"][0]["diagnostics"]["goals"]["7"]["present"])
        self.assertTrue(result["campaigns"][0]["queries"])
        self.assertFalse(result["campaigns"][0]["diagnostics"]["tracking_failure_proven"])

    def test_missing_goal_is_distinct_from_zero_goal(self):
        result = CollectedSource().collect(datetime(2026, 9, 5).date())
        del result["measurement"]["goals"]["7"]
        diagnosis = campaign_diagnostics(result["campaigns"][0], result["measurement"])
        self.assertFalse(diagnosis["goals"]["7"]["present"])
        self.assertEqual(diagnosis["goals"]["7"]["counter_observation"], "goal_not_found_in_counter")

    def test_upgraded_or_supporting_goal_data_triggers_review(self):
        data = snapshot()
        previous = copy.deepcopy(data)
        data["schema_version"] = 2
        self.assertEqual(sufficient(data, previous, POLICY), (True, "analysis_data_upgraded"))
        previous["schema_version"] = 2
        data["campaigns"][0]["current"]["GoalTotals"] = {"8": {"confirmed_sum": 5, "complete": True}}
        self.assertTrue(sufficient(data, previous, POLICY)[0])
        self.assertFalse(sufficient(data, copy.deepcopy(data), POLICY)[0])

    def test_sampler_includes_high_click_zero_cost_and_supporting_conversions(self):
        rows = [{"id": i, "Clicks": 1, "Cost": 100, "Conversions": None, "conversions_by_goal": {"7": None, "8": 2}} for i in range(20)]
        rows.append({"id": 99, "Clicks": 1000, "Cost": 0, "Conversions": None, "conversions_by_goal": {"7": None, "8": None}})
        selected = sample_rows(rows)
        self.assertIn(99, [r["id"] for r in selected])
        self.assertEqual(len(selected), 12)
        self.assertEqual(len({r["id"] for r in selected}), 12)

    def test_card_preserves_named_goals_and_separate_metrics(self):
        data = CollectedSource().collect(datetime(2026, 9, 5).date())
        data["run_id"] = "test"
        body = Actions(FakeSource(), None, POLICY).prepare(proposal("advisory"), data)
        row = {"id": "abcdef123456", "revision": 1, "state": "pending", "body": body}
        self.assertIn("Cart", card(row))
        full = "\n".join(detail_pages(row))
        self.assertIn("12 целевых визитов, 13 достижений", full)
        self.assertIn("основной бизнес-сигнал", full)
        self.assertIn("вспомогательный сигнал, не продажа", full)

    def test_readonly_api_metadata_failure_does_not_authorize_strategy_change(self):
        data = snapshot()
        data["schema_version"] = 2
        data["campaigns"][0]["diagnostics"] = {"counter_linked": True, "goals": {"7": {"present": None}}}
        with self.assertRaisesRegex(ValueError, "Verify campaign counter"):
            Actions(FakeSource(), None, POLICY).prepare(proposal("strategy_value"), data)
        Actions(FakeSource(), None, POLICY).prepare(proposal("ad_text"), data)

    def test_direct_raw_dash_is_preserved_not_reclassified(self):
        direct = SimpleNamespace(_build_reports_url=lambda: "https://api.direct.yandex.com/json/v5/reports", _build_reports_headers=lambda: {})
        raw = b"CampaignId\tImpressions\tClicks\tCost\tConversions_7_AUTO\tConversions_8_AUTO\n1\t100\t20\t200\t--\t5\n"
        class Response:
            status = 200
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def read(self, *args): return raw
        source = DataSource(POLICY, direct=direct, metrika=Mock())
        with patch("marketer.data.urllib.request.urlopen", return_value=Response()):
            rows = source.report(1, "2026-08-01", "2026-08-28", "CAMPAIGN_PERFORMANCE_REPORT", ["CampaignId"], 7, "AUTO", [8])
        self.assertIsNone(rows[0]["Conversions"])
        self.assertEqual(rows[0]["raw_conversions_by_goal"], {"7": "--", "8": "5"})
        raw = raw.replace(b"--\t5", b"--")
        with patch("marketer.data.urllib.request.urlopen", return_value=Response()):
            with self.assertRaisesRegex(ValueError, "Truncated"):
                source.report(1, "2026-08-01", "2026-08-28", "CAMPAIGN_PERFORMANCE_REPORT", ["CampaignId"], 7, "AUTO", [8])

    def test_metrika_rejects_missing_totals(self):
        client = FakeMetrika()
        original = client._get_json
        client._get_json = lambda *a: {**original(*a), "totals": []}
        with self.assertRaisesRegex(ValueError, "Unknown Metrika metric"):
            DataSource(POLICY, direct=Mock(), metrika=client).metrika_report(1, "2026-08-01", "2026-08-28", 7)

    def test_model_requires_coverage_not_only_measurement(self):
        runner = Runner(Mock(), Mock(), Mock(), Mock(), POLICY)
        runner.context = Mock(return_value={"snapshot": {}, "history": []})
        answer = review()
        def response(*a, **k):
            return SimpleNamespace(returncode=0, stdout=json.dumps({"payloads": [{"text": json.dumps(answer)}]}))
        with patch("marketer.runner.subprocess.run", side_effect=response):
            self.assertEqual(set(runner.model({})["coverage"]), AREAS)
            del answer["coverage"]["search"]
            with self.assertRaisesRegex(ValueError, "all marketing areas"):
                runner.model({})

    def test_context_is_readonly_bounded_and_keeps_goal_information(self):
        data = CollectedSource().collect(datetime(2026, 9, 5).date())
        original = copy.deepcopy(data)
        store = Mock()
        store.list.return_value = []
        store.setting.return_value = None
        context = Runner(store, Mock(), Mock(), Mock(), POLICY).context(data)
        self.assertNotIn("daily", context["snapshot"]["campaigns"][0])
        self.assertNotIn("period_reports", context["snapshot"]["campaigns"][0])
        self.assertIn("8", context["snapshot"]["campaigns"][0]["diagnostics"]["goals"])
        self.assertLessEqual(len(json.dumps(context, ensure_ascii=False).encode()), 81000)
        self.assertEqual(data, original)

    def test_primary_conversions_do_not_authorize_supporting_goal_strategy(self):
        data = snapshot()
        data["campaigns"][0]["UnifiedCampaign"]["BiddingStrategy"]["Search"]["PayForConversion"]["GoalId"] = 8
        with self.assertRaisesRegex(ValueError, "Supporting-goal strategy"):
            Actions(FakeSource(), None, POLICY).prepare(proposal("strategy_value"), data)

    def test_evaluation_uses_period_aggregate_and_all_goals(self):
        data = snapshot()
        evidence = {"goal_id": 7, "strategy_goals": [7, 8], "attribution": "AUTO", "current": data["campaigns"][0]["current"]}
        row = {"id": "test", "state": "applied", "applied_at": 1, "evaluated_at": None, "result": {},
               "body": {"campaign_id": 1, "title": "Test", "evaluate_after_days": 28, "evidence": evidence}}
        store, source, telegram = Mock(), Mock(), Mock()
        store.list.return_value = [row]
        source.report.return_value = [{"Clicks": 10, "Impressions": 100, "Cost": 100, "Conversions": 5, "conversions_by_goal": {"7": 5, "8": 8}}]
        Runner(store, source, Mock(), telegram, POLICY).evaluations()
        args = source.report.call_args.args
        self.assertEqual(args[4], ["CampaignId"])
        self.assertEqual(args[7], [7, 8])
        self.assertEqual(store.evaluate.call_args.args[1]["after"]["GoalTotals"]["8"]["confirmed_sum"], 8)

    def test_only_transient_read_error_is_retried(self):
        direct = Mock()
        failure = SimpleNamespace(body={"error": {"error_code": 1000}})
        success = SimpleNamespace(body={"result": {"Campaigns": [{"Id": 1}]}})
        direct.call_v501.side_effect = [failure, success]
        with patch("marketer.data.time.sleep") as sleep:
            result = DataSource(POLICY, direct=direct, metrika=Mock()).entities("campaigns", "Campaigns", {})
        self.assertEqual(result, [{"Id": 1}])
        self.assertEqual(direct.call_v501.call_count, 2)
        self.assertTrue(all(call.args[1] == "get" for call in direct.call_v501.call_args_list))
        sleep.assert_called_once_with(1)
        direct.call_v501.side_effect = [SimpleNamespace(body={"error": {"error_code": 53}})]
        with self.assertRaises(Exception):
            DataSource(POLICY, direct=direct, metrika=Mock()).entities("campaigns", "Campaigns", {})
        direct.call_v501.side_effect = [failure] * 3
        with patch("marketer.data.time.sleep") as sleep:
            with self.assertRaises(Exception):
                DataSource(POLICY, direct=direct, metrika=Mock()).entities("campaigns", "Campaigns", {})
        self.assertEqual(sleep.call_count, 2)

    def test_complex_metrika_query_falls_back_to_single_goals(self):
        client = FakeMetrika()
        original = client._get_json
        calls = []
        def limited(path, params):
            calls.append(params)
            if len(params["metrics"].split(",")) > 7:
                raise YandexMetrikaClientError("too complex", status=400, payload={"errors": [{"error_type": "query_error", "message": "Запрос слишком сложный. Уменьшите интервал."}]})
            return original(path, params)
        client._get_json = limited
        result = DataSource(POLICY, direct=Mock(), metrika=client).metrika_report(1, "2026-08-01", "2026-08-28", 7, extra_goals=[8])
        self.assertEqual(len(calls), 3)
        self.assertTrue(all(p["accuracy"] == "full" for p in calls))
        self.assertEqual(result["totals"]["users"], 90)
        self.assertEqual(result["totals"]["goals"]["8"]["visits"], 12)

    def test_other_metrika_error_is_not_retried(self):
        client = Mock()
        client._get_json.side_effect = YandexMetrikaClientError("bad goal", status=400, payload={"errors": [{"error_type": "query_error", "message": "Invalid goal"}]})
        with self.assertRaises(YandexMetrikaClientError):
            DataSource(POLICY, direct=Mock(), metrika=client).metrika_report(1, "2026-08-01", "2026-08-28", 7, extra_goals=[8])
        client._get_json.assert_called_once()

    def test_oauth_failure_is_actionable_and_never_publishes(self):
        store, telegram = Mock(), Mock()
        runner = Runner(store, Mock(), Mock(), telegram, POLICY)
        runner.context = Mock(return_value={})
        response = SimpleNamespace(returncode=1, stderr="OAuth token refresh failed for openai-codex")
        with patch("marketer.runner.subprocess.run", return_value=response):
            with self.assertRaisesRegex(RuntimeError, "повторного входа"):
                runner.model({})
        self.assertEqual(store.set_setting.call_args.args[1]["status"], "reauthentication_required")
        telegram.publish.assert_not_called()


if __name__ == "__main__":
    unittest.main()
