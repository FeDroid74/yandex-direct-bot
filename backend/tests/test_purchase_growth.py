import copy
from datetime import date
import unittest
from unittest.mock import Mock

from marketer.billing import COUNTER, PURCHASE_GOAL
from marketer.data import DataSource
from marketer.growth import Growth, compare, notice, windows, OBJECTIVE
from marketer.runner import Runner
from services.metrika_client import YandexMetrikaClientError


class PurchaseGrowthTests(unittest.TestCase):
    def setUp(self):
        self.policy = {"goal_id": PURCHASE_GOAL, "counter_id": COUNTER, "conversion_lag_days": 7}
        self.snapshot = {"measurement": {"catalog_complete": True, "goals": {str(PURCHASE_GOAL): {
            "type": "e_purchase", "status": "Active"}}, "counter": {"time_zone_name": "Europe/Moscow"}}}
        self.source = Mock()
        # Includes a campaign absent from current ON/SUSPENDED collection.
        self.source.entities.return_value = [{"Id": 1}, {"Id": 2}, {"Id": 3}]

    def reports(self, values):
        def report(start, end, goal, **kw):
            self.assertEqual(goal, PURCHASE_GOAL)
            return {"sampled": False, "totals": {"goals": {str(goal): {"reaches": values[start[:7]]}}}}
        self.source.purchase_report.side_effect = report

    def test_calendar_lag_and_year_rollover(self):
        cutoff, months, progress = windows(date(2026, 11, 13), 7)
        self.assertEqual(cutoff, "2026-11-05")
        self.assertEqual([m["month"] for m in months], ["2026-08", "2026-09", "2026-10"])
        self.assertEqual(progress[0]["to"], "2026-10-05")
        self.assertEqual(progress[1]["to"], "2026-11-05")
        self.assertEqual(windows(date(2027, 1, 3), 7)[1][-1]["month"], "2026-11")
        self.assertEqual(windows(date(2026, 3, 8), 7)[1][-1]["to"], "2026-02-28")
        self.assertEqual(windows(date(2026, 11, 3), 7)[2], [])

    def test_primary_direct_scope_and_site_are_never_added(self):
        self.reports({"2026-08": 2, "2026-09": 3, "2026-10": 4, "2026-11": 5})
        r = Growth(self.source, self.policy).collect(self.snapshot, date(2026, 11, 13))
        self.assertEqual(r["comparison"], compare(3, 4))
        self.assertFalse(r["diagnosis_required"])
        self.assertEqual(r["next_month_target"]["at_least"], 5)
        self.assertEqual(r["campaign_ids"], [1, 2, 3])
        self.source.entities.assert_called_once_with("campaigns", "Campaigns", {"SelectionCriteria": {}, "FieldNames": ["Id"]})
        calls = self.source.purchase_report.call_args_list
        self.assertEqual(len(calls), 10)
        self.assertEqual(calls[0].kwargs["campaign_ids"], [1, 2, 3])
        self.assertIsNone(calls[1].kwargs["campaign_ids"])
        self.assertFalse(OBJECTIVE["paid_orders_confirmed"])

    def test_stagnation_and_zero_baseline_not_fake_growth(self):
        self.reports({"2026-08": 0, "2026-09": 0, "2026-10": 0, "2026-11": 0})
        r = Growth(self.source, self.policy).collect(self.snapshot, date(2026, 11, 13))
        self.assertEqual(r["stagnant_months"], 2)
        self.assertTrue(r["diagnosis_required"])
        self.assertEqual(r["comparison"]["status"], "flat")
        self.assertIn("роста нет", notice(r))
        self.assertIsNone(compare(0, 1)["percent"])

    def test_incomplete_sampling_or_unknown_is_not_zero(self):
        for bad in ({"sampled": True}, {"sampled": False, "totals": {}}, {"sampled": False, "totals": {
                "goals": {str(PURCHASE_GOAL): {"reaches": float('nan')}}}}):
            with self.subTest(bad=bad):
                self.source.purchase_report.side_effect = None
                self.source.purchase_report.return_value = bad
                r = Growth(self.source, self.policy).collect(self.snapshot, date(2026, 11, 13))
                self.assertEqual(r["status"], "data_unavailable")
                self.assertIsNone(r["comparison"]["after"])
                self.assertTrue(r["diagnosis_required"])

    def test_goal_counter_timezone_and_scope_fail_closed(self):
        for patch in ({"goal_id": 123}, {"counter_id": 42}, {"report_timezone": "UTC"}, {"campaign_ids": [77]}):
            with self.subTest(patch=patch):
                r = Growth(self.source, {**self.policy, **patch}).collect(self.snapshot, date(2026, 11, 13))
                self.assertEqual(r["status"], "data_unavailable")
                self.source.purchase_report.assert_not_called()
        s = copy.deepcopy(self.snapshot)
        s["measurement"]["goals"][str(PURCHASE_GOAL)]["type"] = "number"
        self.assertEqual(Growth(self.source, self.policy).collect(s, date(2026, 11, 13))["status"], "data_unavailable")

    def test_model_cannot_claim_growth_or_invent_course_source(self):
        self.reports({"2026-08": 2, "2026-09": 2, "2026-10": 2, "2026-11": 0})
        s = {"purchase_growth": Growth(self.source, self.policy).collect(self.snapshot, date(2026, 11, 13))}
        g = {"status": "flat", "diagnosis": "Two months had 2 purchases each.", "next_step": "Review one landing hypothesis.", "knowledge_refs": []}
        Runner.validate_growth({"growth_review": g}, s)
        for bad in ({**g, "status": "increased"}, {**g, "next_step": ""}, {**g, "knowledge_refs": ["invented"]}):
            with self.assertRaises(ValueError):
                Runner.validate_growth({"growth_review": bad}, s)

    def test_multi_campaign_filter_preserves_account_scope(self):
        api = Mock()
        api._get_json.return_value = {"sampled": False, "total_rows": 0, "data": [], "totals": [0] * 7}
        s = DataSource(self.policy, direct=Mock(), metrika=api)
        report = s.metrika_report(None, "2026-10-01", "2026-10-31", PURCHASE_GOAL, campaign_ids=[2, 1])
        self.assertEqual(report["attribution"], "lastsign")
        self.assertEqual(api._get_json.call_args.args[1]["filters"], "(ym:s:lastsignDirectClickOrder=='1' OR ym:s:lastsignDirectClickOrder=='2')")
        with self.assertRaises(ValueError):
            s.metrika_report(None, "2026-10-01", "2026-10-31", PURCHASE_GOAL, campaign_ids=[])

    def test_complex_purchase_query_splits_disjoint_dates_without_sampling(self):
        api = Mock()
        error = YandexMetrikaClientError("complex", status=400, payload={"errors": [{
            "error_type": "query_error", "message": "Запрос слишком сложный. Уменьшите интервал."}]})
        api._get_json.side_effect = [error, {"sampled": False, "totals": [2]}, {"sampled": False, "totals": [3]}]
        s = DataSource(self.policy, direct=Mock(), metrika=api)
        r = s.purchase_report("2026-10-01", "2026-10-31", PURCHASE_GOAL, [1, 2])
        self.assertEqual(r["totals"]["goals"][str(PURCHASE_GOAL)]["reaches"], 5)
        self.assertEqual(r["partitions"], [{"from": "2026-10-01", "to": "2026-10-16"}, {"from": "2026-10-17", "to": "2026-10-31"}])
        for c in api._get_json.call_args_list:
            self.assertEqual(c.args[1]["accuracy"], "full")
            self.assertIn("filters", c.args[1])

    def test_sampled_partition_invalidates_entire_month(self):
        api = Mock()
        api._get_json.return_value = {"sampled": True, "totals": [2]}
        with self.assertRaises(ValueError):
            DataSource(self.policy, direct=Mock(), metrika=api).purchase_report("2026-10-01", "2026-10-31", PURCHASE_GOAL, [1])

    def test_observed_opportunity_survives_model_row_sampling(self):
        from test_opportunities import ReviewTests
        case = ReviewTests()
        case.setUp()
        data = case.s
        import time
        data["created"] = time.time()
        data["campaigns"][0].update(groups=[], ad_performance=[], goal_id=PURCHASE_GOAL,
                                    previous={"Conversions": 0}, attribution="AUTO")
        data["campaigns"][0]["UnifiedCampaign"]["BiddingStrategy"]["Search"]["PayForConversion"].update(Cpa=1000000000, WeeklySpendLimit=20000000000)
        store = Mock()
        store.setting.return_value = None
        context = Runner(store, Mock(), Mock(), Mock(), {}).context(data, decisions={"items": [], "counts": {}})
        self.assertEqual(context["observed_opportunities"][0]["action"]["ad_id"], case.aid)
        self.assertIn("посуда", context["observed_opportunities"][0]["after"])
