import copy
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]/"deploy"))
from configure_model import configure, MODEL_REF, MODEL_ID


class ModelConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.config = {"models": {"providers": {"openai-codex": {
            "api": "openai-codex-responses", "auth": "oauth", "baseUrl": "https://chatgpt.com/backend-api",
            "models": [{"id": "gpt-5.6-terra"}]}}}, "agents": {
                "defaults": {"model": {"primary": "unrelated-default"}}, "list": [
                    {"id": "direct", "model": "old-direct", "tools": {"alsoAllow": ["marketer_review"]}},
                    {"id": "direct-marketer", "model": "old-analyst", "tools": {"deny": ["*"]}},
                    {"id": "other", "model": "other-model"}]}}

    def test_analyst_first_keeps_interactive_and_other_agents_unchanged(self):
        original = copy.deepcopy(self.config)
        configure(self.config)
        self.assertEqual(self.config["agents"]["list"][0], original["agents"]["list"][0])
        self.assertEqual(self.config["agents"]["list"][1]["model"], MODEL_REF)
        self.assertEqual(self.config["agents"]["list"][1]["tools"], {"deny": ["*"]})
        self.assertEqual(self.config["agents"]["list"][2], original["agents"]["list"][2])
        self.assertEqual(self.config["agents"]["defaults"]["model"], original["agents"]["defaults"]["model"])

    def test_direct_switch_is_scoped_idempotent_and_preserves_oauth(self):
        configure(self.config, True)
        once = copy.deepcopy(self.config)
        configure(self.config, True)
        self.assertEqual(self.config, once)
        self.assertEqual(self.config["agents"]["list"][0]["model"], MODEL_REF)
        provider = self.config["models"]["providers"]["openai-codex"]
        self.assertEqual(provider["auth"], "oauth")
        self.assertEqual(provider["models"][0]["id"], "gpt-5.6-terra")
        self.assertEqual([m["id"] for m in provider["models"]].count(MODEL_ID), 1)
        self.assertNotIn("openai", self.config["models"]["providers"])
