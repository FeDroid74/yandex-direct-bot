import copy
import io
import json
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from marketer.actions import Actions
from marketer.data import DataSource, sufficient, totals
from marketer.store import Conflict, Store
from marketer.telegram import action_text, buttons, card, detail_pages, detail_response, split_text, summary, telegram_length
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


class CardTests(unittest.TestCase):
    def row(self, kind="advisory"):
        body = Actions(FakeSource(), None, POLICY).prepare(proposal(kind), snapshot())
        return {"id": "abcdef123456", "revision": 1, "state": "pending", "body": body}

    def test_full_manual_criterion_and_single_hypothesis_label(self):
        row = self.row()
        body = row["body"]
        body["expected_effect"] = "Гипотеза: Гипотеза: тестовое действие видно в Метрике."
        body["success_metric"] = "Успех — " + "цель отображается корректно; " * 10 + "проверить повторно."
        original = copy.deepcopy(row)
        for output in (card(row), detail_response(row)["text"]):
            self.assertEqual(output.count("Гипотеза:"), 1)
            self.assertIn(body["success_metric"], output)
            self.assertIn("Критерий выполнения:", output)
            self.assertNotIn("Оценка через", output)
            self.assertNotIn("Исходные параметры", output)
            self.assertNotIn("\\", output)
        self.assertEqual(row, original)

    def test_automatic_action_keeps_evaluation_window(self):
        row = self.row("strategy_value")
        self.assertIn("Оценка через 28 дней:", card(row))
        self.assertIn("Исходные параметры:", detail_response(row)["text"])

    def test_unknown_zero_is_not_reported_as_zero_conversions(self):
        row = self.row()
        row["body"]["evidence"]["current"].update(Conversions=0, ConversionsComplete=False)
        for output in (card(row), detail_response(row)["text"]):
            self.assertIn("итог неизвестен", output)
            self.assertIn("Считать это нулём нельзя", output)
            self.assertNotIn("не менее 0", output)
            self.assertNotIn("0 целевых визитов", output)

    def test_known_zero_and_positive_lower_bound(self):
        row = self.row()
        stats = row["body"]["evidence"]["current"]
        stats.update(Conversions=0, ConversionsComplete=True)
        self.assertIn("0 целевых визитов", card(row))
        stats.update(Conversions=2, ConversionsComplete=False)
        self.assertIn("подтверждено не менее 2 целевых визитов", card(row))

    def test_summary_uses_whole_words_and_explicit_ellipsis(self):
        self.assertEqual(summary("Один два три четыре", 12), "Один два…")
        self.assertEqual(summary("Один два", 12), "Один два")
        self.assertEqual(summary("оченьдлинноесловобезпробелов", 12), "…")

    def test_pagination_preserves_all_text_and_unicode(self):
        text = ("Факты: " + chr(0x1F600) + "\nполные данные " * 50 + "\n\n") * 40
        pages = split_text(text, 400)
        self.assertEqual("".join(pages), text)
        self.assertTrue(all(telegram_length(page) <= 400 for page in pages))
        unbroken = chr(0x1F600) * 1000
        self.assertEqual("".join(split_text(unbroken, 401)), unbroken)

    def test_long_details_have_versioned_navigation_and_full_ending(self):
        row = self.row()
        row["body"]["reason"] = "Наблюдаемые факты. " * 200
        row["body"]["success_metric"] = "Полный критерий успеха. " * 60 + "ПОСЛЕДНЕЕ СЛОВО."
        row["body"]["evidence"]["limits"] = ["Важное ограничение. " * 100]
        row["result"] = {"error": "Диагностика. " * 200 + "КОНЕЦ РЕЗУЛЬТАТА."}
        pages = detail_pages(row)
        self.assertGreater(len(pages), 1)
        all_text = "".join(page.split("\n\n", 1)[1] for page in pages)
        for key in ("reason", "success_metric"):
            self.assertIn(row["body"][key], all_text)
        self.assertIn(row["result"]["error"], all_text)
        self.assertIn(row["body"]["evidence"]["limits"][0], all_text)
        for index, page in enumerate(pages, 1):
            self.assertLessEqual(telegram_length(page), 4000)
            response = detail_response(row, index)
            self.assertEqual(response["text"], page)
            expected_targets = [i for i in (index - 1, index + 1) if 1 <= i <= len(pages)]
            self.assertEqual([int(b["callback_data"].split()[-1]) for b in response["buttons"][0]], expected_targets)
            for button in response["buttons"][0]:
                self.assertIn("/yd details abcdef123456 1 ", button["callback_data"])
                self.assertLessEqual(len(button["callback_data"].encode()), 64)
        self.assertIn(action_text(row["body"]), card(row))
        for warning in row["body"]["warnings"]:
            self.assertIn(warning, card(row))

    def test_invalid_page_is_rejected(self):
        row = self.row()
        for value in (0, -1, 2, True, 1.0, "1", None):
            with self.assertRaises(ValueError):
                detail_response(row, value)
        self.assertEqual(detail_response(row)["buttons"], [])

    def test_oversized_exact_action_is_rejected_before_storage(self):
        raw = proposal("advisory")
        raw["action"]["task"] = chr(0x1F600) * 1800
        raw["title"] = "Название " * 20
        with self.assertRaisesRegex(ValueError, "Точное действие"):
            Actions(FakeSource(), None, POLICY).prepare(raw, snapshot())

    def test_main_card_keeps_long_action_and_all_warnings(self):
        row = self.row()
        row["body"]["action"]["task"] = "Проверить вручную. " * 95
        row["body"]["reason"] = "Наблюдаемые факты. " * 88
        row["body"]["expected_effect"] = "Ожидаемое улучшение. " * 38
        row["body"]["success_metric"] = "Тестовое действие. " * 40
        output = card(row)
        self.assertLessEqual(telegram_length(output), 4000)
        self.assertIn(action_text(row["body"]), output)
        self.assertIn("\n".join(row["body"]["warnings"]), output)


class CardEndpointTests(unittest.TestCase):
    def setUp(self):
        import marketer_service
        self.row = CardTests().row()
        self.row["body"]["evidence"]["limits"] = ["Полное ограничение. " * 400]
        self.store = Mock()
        self.store.get.return_value = self.row
        self.actions, self.telegram = Mock(), Mock()
        config = {"api_key": "read-test", "decision_key": "decision-test", "owner_id": "42"}

        def server(_address, handler):
            self.handler_class = handler
            return SimpleNamespace(serve_forever=lambda: None)

        with patch.object(marketer_service, "build_app", return_value=(self.store, self.actions, None, self.telegram, config)), \
                patch.object(marketer_service, "ThreadingHTTPServer", side_effect=server):
            marketer_service.serve()

    def request(self, **overrides):
        params = {"id": self.row["id"], "revision": 1, "decision": "details", "sender_id": "42", "channel": "telegram", **overrides}
        data = json.dumps(params).encode()
        handler = self.handler_class.__new__(self.handler_class)
        handler.path = "/decision"
        handler.headers = {"Authorization": "Bearer decision-test", "Content-Length": str(len(data))}
        handler.rfile, handler.wfile = io.BytesIO(data), io.BytesIO()
        handler.send_response, handler.send_header, handler.end_headers = Mock(), Mock(), Mock()
        handler.do_POST()
        return handler.send_response.call_args.args[0], json.loads(handler.wfile.getvalue())

    def test_paged_read_endpoint_never_executes_or_sends(self):
        status, response = self.request(page=2)
        self.assertEqual(status, 200)
        self.assertEqual(response["page"], 2)
        self.assertIn("Подробности · 2/", response["text"])
        self.assertTrue(response["buttons"])
        self.actions.decide.assert_not_called()
        self.telegram.refresh.assert_not_called()

    def test_page_cannot_be_used_with_a_mutation(self):
        for decision in ("approve", "reject", "defer", "edit", "done"):
            self.assertEqual(self.request(decision=decision, page=1)[0], 400)
        self.actions.decide.assert_not_called()

    def test_details_require_current_revision_and_owner(self):
        self.assertEqual(self.request(revision=2)[0], 409)
        self.assertEqual(self.request(sender_id="43")[0], 403)
        self.assertEqual(self.request(channel="discord")[0], 403)
        for page in (True, "1", 0, 999):
            self.assertEqual(self.request(page=page)[0], 400)
        self.actions.decide.assert_not_called()


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
