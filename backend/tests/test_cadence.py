import sys
import unittest
import tempfile
from datetime import datetime
from pathlib import Path
from marketer.schedule import review_slot
from marketer.store import Store
from marketer.runner import light_candidates, weekly_traffic

sys.path.insert(0, str(Path(__file__).resolve().parents[2]/"deploy"))
from openclaw_reply_guard import CHANGES, transform
from install_marketer import configure_chat


class CadenceTests(unittest.TestCase):
    def test_single_persistent_calendar_across_week_and_month(self):
        def slot(date):
            return review_slot(datetime.fromisoformat(date+"+00:00").timestamp(), "Europe/Podgorica")
        self.assertEqual(slot("2026-11-12T10:59"), ("2026-11-09", True, False))
        self.assertEqual(slot("2026-11-12T11:00"), ("2026-11-12", False, True))
        self.assertEqual(slot("2026-11-14T11:00"), ("2026-11-12", False, True))
        self.assertEqual(slot("2026-11-15T11:00"), ("2026-11-15", False, True))
        self.assertEqual(slot("2026-11-16T11:00"), ("2026-11-16", True, False))
        self.assertEqual(slot("2026-11-30T11:00"), ("2026-11-30", True, True))
        self.assertEqual(slot("2026-12-03T11:00"), ("2026-12-03", False, True))

    def test_light_model_not_called_for_click_billing_or_missing_evidence(self):
        self.assertEqual(light_candidates([{"State": "ON", "queries": [{"Clicks": 150}]}]), [])

    def test_catchup_never_loses_weekly_review_or_replays_same_slot(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp)/"state.db")
            self.assertEqual(store.claim_reviews("2026-11-18", "2026-11-16", True), (True, True))
            self.assertEqual(store.claim_reviews("2026-11-18", "2026-11-16", True), (False, False))
            self.assertEqual(store.claim_reviews("2026-11-21", "2026-11-16", True), (False, True))
            self.assertEqual(store.claim_reviews("2026-11-30", "2026-11-30", True), (True, True))

    def test_runtime_guard_idempotent_and_rejects_unknown_bundle(self):
        original = "\n".join(a for a, _ in CHANGES)
        replaced = transform(original)
        self.assertEqual(transform(replaced), replaced)
        self.assertIn("if (timedOutDuringCompaction ||", replaced)
        with self.assertRaises(ValueError):
            transform(original.replace(CHANGES[0][0], "upstream changed"))

    def test_only_chat_gets_course_helpers(self):
        cfg = {"channels": {"telegram": {}}, "agents": {"list": [{"id": "direct"},
               {"id": "direct-marketer", "tools": {"deny": ["*"]}}]}}
        configure_chat(cfg)
        configure_chat(cfg)
        tools = cfg["agents"]["list"][0]["tools"]["alsoAllow"]
        self.assertIn("marketer_knowledge_status", tools)
        self.assertEqual(len(set(tools)), len(tools))
        self.assertNotIn("marketer_propose", tools)
        self.assertEqual(cfg["agents"]["list"][1]["tools"], {"deny": ["*"]})

    def test_weekly_unknown_not_zero_and_no_all_goals_substitution(self):
        data = {"date_to": "2026-11-04", "campaigns": [{"Name":"Test", "daily":[
            {"Date":"2026-11-03","Clicks":35,"Cost":199,"Impressions":300,"Conversions":9,
             "conversions_by_goal":{"352606262": None, "other": 9}}]}]}
        out = weekly_traffic(data)
        self.assertIn("нет строк", out)
        self.assertIn("покупок: неизвестно", out)
        self.assertNotIn("покупок: 9", out)
