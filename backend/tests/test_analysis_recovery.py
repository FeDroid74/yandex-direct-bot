import copy
import io
import json
import tempfile
import time
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import Mock, patch

from marketer.actions import Actions
from marketer.analyst import Analyst, AnalystUnavailable, NotDispatched
from marketer.data import DataSource
from marketer.errors import ReadError, safe_error
from marketer.runner import Runner
from marketer.store import Conflict, Store
from test_marketer import FakeSource, POLICY, proposal, snapshot
from test_marketer_stage2 import review


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="analysis-test-")
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(Path(self.tmp.name) / "state.sqlite3")
        self.source = FakeSource()
        self.source.collect = Mock(side_effect=snapshot)
        self.telegram = Mock()
        self.actions = Actions(self.source, self.store, POLICY)
        self.runner = Runner(self.store, self.source, self.actions, self.telegram, POLICY)

    def test_connect_failure_before_rpc_is_distinct_from_lost_acknowledgement(self):
        analyst = Analyst(self.store, POLICY)
        for stderr, expected in [("gateway connect failed: Error: gateway closed (1000):\n", NotDispatched),
                                 ("gateway connect failed: Error: gateway not connected\n", NotDispatched),
                                 ("Gateway call failed: Error: gateway closed (1000):\n", AnalystUnavailable)]:
            completed = Mock(returncode=1, stdout="", stderr=stderr)
            with patch("marketer.analyst.subprocess.run", return_value=completed):
                with self.assertRaises(expected) as error:
                    analyst.rpc("agent", {})
            self.assertIs(type(error.exception), expected)

    def test_new_monthly_evidence_opens_read_only_analysis_gate(self):
        self.runner.policy = {**POLICY, "monthly_purchase_objective": True}
        self.runner.model = Mock(return_value={"summary": "Review", "coverage": {}, "proposals": [], "growth_review": {
            "status": "flat", "diagnosis": "No growth was observed.", "next_step": "Check one hypothesis."}})
        growth = {"status": "ready", "comparison": {"status": "flat"}, "fingerprint": "monthly-new"}
        with patch("marketer.runner.sufficient", return_value=(False, "insufficient_new_data")), \
                patch("marketer.runner.Growth.collect", return_value=growth):
            result = self.runner.run(force=True, publish=False)
        self.assertEqual(result["status"], "preview_ready")
        self.runner.model.assert_called_once()
        self.assertEqual(self.store.get_run(result["run_id"])["data"]["gate"], "new_monthly_purchase_evidence")
        self.assertEqual(self.source.writes, 0)
        self.telegram.publish.assert_not_called()

    def test_only_known_unsubmitted_connection_is_retried_with_same_id(self):
        analyst = Analyst(self.store, POLICY)
        analyst.rpc = Mock(side_effect=[NotDispatched("connect"), {"payloads": [{"text": '{"ok":true}'}]}])
        with patch("marketer.analyst.time.sleep"):
            self.assertEqual(analyst.call("r", "b", {}, "p", lambda x:x), {"ok": True})
        calls = analyst.rpc.call_args_list
        self.assertEqual([c.args[0] for c in calls], ["agent", "agent"])
        self.assertEqual(calls[0].args[1]["idempotencyKey"], calls[1].args[1]["idempotencyKey"])

    def test_failed_history_connection_does_not_prove_agent_not_dispatched(self):
        analyst = Analyst(self.store, POLICY)
        completed = Mock(returncode=1, stdout="", stderr="gateway connect failed: Error: gateway closed (1000):\n")
        with patch("marketer.analyst.subprocess.run", return_value=completed), self.assertRaises(AnalystUnavailable) as err:
            analyst.rpc("chat.history", {})
        self.assertNotIsInstance(err.exception, NotDispatched)

    def test_scheduled_read_recovery_reuses_run_and_collection(self):
        self.runner.model = Mock(side_effect=[NotDispatched("gateway closed"), review()])
        with patch("marketer.runner.review_slot", return_value=("2026-11-16", True, False)), patch("marketer.runner.time.sleep"):
            result = self.runner.run(scheduled=True)
        self.assertEqual(result["status"], "complete")
        self.assertEqual(self.runner.model.call_args_list[0], self.runner.model.call_args_list[1])
        self.assertEqual(len(self.store.runs()), 1)
        self.source.collect.assert_called_once()
        self.assertEqual(self.source.writes, 0)

    def test_exhausted_preconnect_failure_does_not_wait_for_nonexistent_model(self):
        analyst = Analyst(self.store, POLICY)
        analyst.rpc = Mock(side_effect=NotDispatched("connect"))
        with patch("marketer.analyst.time.sleep"), self.assertRaises(NotDispatched):
            analyst.call("r", "b", {}, "p", lambda x:x)
        self.assertEqual(analyst.rpc.call_count, 3)
        self.assertEqual(self.store.get_analysis_step("r", "b")["status"], "not_dispatched")

    def test_saved_preconnect_error_reconciles_empty_session_before_submission(self):
        step = self.store.analysis_step("r", "b", "p\nДАННЫЕ:\n{}", POLICY["analyst_agent"])
        self.store.step_update("r", "b", "uncertain", error="OpenClaw agent:\ngateway connect failed: Error: gateway closed (1000):\n")
        analyst = Analyst(self.store, POLICY)
        analyst.rpc = Mock(side_effect=[{"messages": []}, {"payloads": [{"text": '{"ok":true}'}]}])
        analyst.call("r", "b", {}, "p", lambda x:x)
        self.assertEqual([c.args[0] for c in analyst.rpc.call_args_list], ["chat.history", "agent"])
        self.assertEqual(analyst.rpc.call_args_list[1].args[1]["idempotencyKey"], step["request_id"])

    def test_new_dispatch_does_not_inherit_old_preconnect_error(self):
        self.store.analysis_step("r", "b", "message", POLICY["analyst_agent"])
        self.store.step_update("r", "b", "not_dispatched", error="old preconnect failure")
        self.assertTrue(self.store.claim_analysis_step("r", "b"))
        self.assertIsNone(self.store.get_analysis_step("r", "b")["error"])

    def test_large_account_is_split_without_losing_campaigns_or_raw_data(self):
        data = snapshot()
        original = data["campaigns"][0]
        data["campaigns"] = [{**copy.deepcopy(original), "Id": i, "Name": "Campaign " + str(i)} for i in range(1, 101)]
        before = copy.deepcopy(data)
        batches = self.runner.contexts(data)
        self.assertGreater(len(batches), 1)
        self.assertEqual([c["Id"] for b in batches for c in b["snapshot"]["campaigns"]], list(range(1, 101)))
        self.assertTrue(all(self.runner.context_size(b) < 71000 for b in batches))
        self.assertEqual(data, before)
        self.assertTrue(self.runner.context_page(data)["overview_only"])
        self.assertEqual(self.runner.context_page(data, 2), batches[1])
        with self.assertRaises(ValueError):
            self.runner.context_page(data, True)

    def test_group_sampling_is_explicit_and_unknown_metrics_survive(self):
        data = snapshot()
        c = data["campaigns"][0]
        c["groups"] = [{"Id": i} for i in range(1000)]
        c["current"].update(Conversions=None, ConversionsComplete=False, CPA=None)
        context = self.runner.context(data)["snapshot"]["campaigns"][0]
        self.assertEqual(context["groups_total_rows"], 1000)
        self.assertEqual(len(context["groups"]), 60)
        self.assertIsNone(context["current"]["Conversions"])
        self.assertFalse(context["current"]["ConversionsComplete"])

    def test_checkpoint_survives_failure_and_resume_does_not_recollect(self):
        self.runner.model = Mock(side_effect=AnalystUnavailable("connection lost"))
        with self.assertRaises(AnalystUnavailable):
            self.runner.run(force=True, publish=False)
        failed = self.store.runs(1)[0]
        self.assertEqual(failed["data"]["stage"], "analysis")
        self.assertEqual(len(failed["data"]["snapshot"]["campaigns"]), 1)
        self.runner.model = Mock(return_value=review())
        result = self.runner.run(resume_run_id=failed["id"], publish=False)
        self.assertEqual(result["status"], "preview_ready")
        self.source.collect.assert_called_once()
        self.telegram.publish.assert_not_called()
        self.telegram.notify.assert_not_called()
        self.assertEqual(self.store.list(), [])
        self.assertEqual(self.source.writes, 0)

    def test_preview_then_publish_uses_saved_analysis_once(self):
        self.runner.model = Mock(return_value={**review(), "proposals": [proposal("advisory")]})
        preview = self.runner.run(force=True, publish=False)
        def publish(row, store):
            store.delivery(row["id"], "sent", 1)
        self.telegram.publish.side_effect = publish
        result = self.runner.run(resume_run_id=preview["run_id"])
        self.assertEqual(result["status"], "complete")
        self.runner.model.assert_called_once()
        self.source.collect.assert_called_once()
        self.telegram.publish.assert_called_once()
        self.assertEqual(self.store.list()[0]["state"], "pending")
        self.assertEqual(self.source.writes, 0)

    def test_interrupted_publication_does_not_send_confirmed_card_twice(self):
        self.runner.model = Mock(return_value={**review(), "proposals": [proposal("advisory")]})
        def sent_then_lost(row, store):
            store.delivery(row["id"], "sent", 99)
            raise RuntimeError("process interrupted after confirmed send")
        self.telegram.publish.side_effect = sent_then_lost
        with self.assertRaises(RuntimeError):
            self.runner.run(force=True)
        failed = self.store.runs(1)[0]
        result = self.runner.run(resume_run_id=failed["id"])
        self.assertEqual(result["status"], "complete")
        self.telegram.publish.assert_called_once()
        self.runner.model.assert_called_once()
        self.assertEqual(len(self.store.list()), 1)

    def test_uncertain_publication_is_not_repeated(self):
        self.runner.model = Mock(return_value={**review(), "proposals": [proposal("advisory")]})
        def uncertain(row, store):
            store.delivery(row["id"], "uncertain")
            raise TimeoutError("delivery unknown")
        self.telegram.publish.side_effect = uncertain
        with self.assertRaises(TimeoutError):
            self.runner.run(force=True)
        failed = self.store.runs(1)[0]
        result = self.runner.run(resume_run_id=failed["id"])
        self.assertEqual(result["status"], "delivery_uncertain")
        self.telegram.publish.assert_called_once()

    def test_health_distinguishes_last_success_from_latest_failure(self):
        self.store.save_run("success", "complete", {"completed_at": 123, "snapshot": snapshot()})
        self.store.save_run("failure", "running", {"snapshot": snapshot()})
        self.store.save_run("failure", "failed", {"stage": "analysis", "error": "test"})
        health = self.store.analysis_health()
        self.assertEqual(health["last_successful_analysis"], {"id": "success", "at": 123})
        self.assertEqual(health["latest_attempt"]["status"], "failed")
        self.assertIn("snapshot", self.store.get_run("failure")["data"])

    def test_recovery_marks_all_active_stages_interrupted(self):
        for status in ("running", "collected", "analyzing", "publishing"):
            self.store.save_run(status, status, {"snapshot": snapshot()})
        self.store.recover()
        self.assertEqual({r["status"] for r in self.store.runs(10)}, {"interrupted"})

    def test_analyst_request_is_cached_and_never_sent_twice(self):
        analyst = Analyst(self.store, POLICY)
        analyst.rpc = Mock(return_value={"payloads": [{"text": json.dumps(review())}]})
        result = analyst.call("run", "batch", {}, "prompt", self.runner.validate_review)
        self.assertEqual(result, review())
        self.assertEqual(analyst.call("run", "batch", {}, "prompt", self.runner.validate_review), result)
        analyst.rpc.assert_called_once()
        with self.assertRaises(Conflict):
            analyst.call("run", "batch", {"changed": True}, "prompt", self.runner.validate_review)

    def test_lost_ack_is_recovered_from_session_without_resubmission(self):
        analyst = Analyst(self.store, POLICY)
        def rpc(method, params, timeout=40):
            if method == "agent":
                raise AnalystUnavailable("connection lost")
            self.assertEqual(method, "chat.history")
            return {"messages": [{"role": "assistant", "stopReason": "stop", "content": [{"type": "text", "text": json.dumps(review())}]}]}
        analyst.rpc = Mock(side_effect=rpc)
        self.assertEqual(analyst.call("run", "batch", {}, "prompt", self.runner.validate_review), review())
        self.assertEqual([c.args[0] for c in analyst.rpc.call_args_list], ["agent", "chat.history"])

    def test_uncertain_request_resumes_by_read_only_calls(self):
        analyst = Analyst(self.store, POLICY)
        analyst.rpc = Mock(side_effect=AnalystUnavailable("offline"))
        with patch("marketer.analyst.time.sleep"):
            with self.assertRaises(AnalystUnavailable):
                analyst.call("run", "batch", {}, "prompt", self.runner.validate_review)
        self.assertEqual(self.store.get_analysis_step("run", "batch")["status"], "uncertain")
        analyst.rpc = Mock(return_value={"messages": [{"role": "assistant", "content": json.dumps(review())}]})
        self.assertEqual(analyst.call("run", "batch", {}, "prompt", self.runner.validate_review), review())
        self.assertEqual([c.args[0] for c in analyst.rpc.call_args_list], ["chat.history"])

    def test_invalid_model_result_stays_blocked(self):
        analyst = Analyst(self.store, POLICY)
        analyst.rpc = Mock(return_value={"payloads": [{"text": "not json"}]})
        with self.assertRaises(ValueError):
            analyst.call("run", "batch", {}, "prompt", self.runner.validate_review)
        with self.assertRaises(AnalystUnavailable):
            analyst.call("run", "batch", {}, "prompt", self.runner.validate_review)
        analyst.rpc.assert_called_once()

    def test_step_submission_claim_is_atomic(self):
        self.store.analysis_step("run", "batch", "message", "test")
        self.assertTrue(self.store.claim_analysis_step("run", "batch"))
        self.assertFalse(self.store.claim_analysis_step("run", "batch"))

    def test_portfolio_selection_preserves_actions_and_completed_batches(self):
        data = snapshot()
        second = copy.deepcopy(data["campaigns"][0])
        second["Id"] = 2
        data["campaigns"].append(second)
        batches = []
        for c in data["campaigns"]:
            batches.append({"snapshot": {"campaigns": [c]}, "scope": {"campaign_ids": [c["Id"]]}})
        self.runner.contexts = Mock(return_value=batches)
        def rpc(method, params, timeout=40):
            self.assertEqual(method, "agent")
            context = json.loads(params["message"].split("\nДАННЫЕ:\n", 1)[1])
            if "candidates" in context:
                result = {"summary": "Portfolio", "coverage": review()["coverage"], "selected_ids": [context["candidates"][0]["id"]]}
            else:
                candidate = proposal("advisory")
                candidate["campaign_id"] = context["scope"]["campaign_ids"][0]
                result = {**review(), "proposals": [candidate]}
            return {"payloads": [{"text": json.dumps(result)}]}
        with patch.object(Analyst, "rpc", side_effect=rpc) as call:
            result = self.runner.model(data)
            self.assertEqual(result["proposals"], [proposal("advisory")])
            self.assertEqual(call.call_count, 3)
            self.assertEqual(self.runner.model(data), result)
            self.assertEqual(call.call_count, 3)

    def test_waiting_for_existing_model_run_does_not_submit_again(self):
        analyst = Analyst(self.store, POLICY)
        methods = []
        histories = iter([{"messages": []}, {"messages": [{"role": "assistant", "content": json.dumps(review())}]}])
        def rpc(method, params, timeout=40):
            methods.append(method)
            if method == "agent":
                return {"runId": params["idempotencyKey"], "status": "accepted"}
            if method == "chat.history":
                return next(histories)
            return {"runId": params["runId"], "status": "ok"}
        analyst.rpc = rpc
        self.assertEqual(analyst.call("run", "batch", {}, "prompt", self.runner.validate_review), review())
        self.assertEqual(methods, ["agent", "chat.history", "agent.wait", "chat.history"])

    def test_oversize_single_campaign_is_not_silently_omitted(self):
        data = snapshot()
        data["campaigns"][0]["UnifiedCampaign"]["huge_metadata"] = "x" * 200000
        with self.assertRaisesRegex(ValueError, "no campaigns were silently discarded"):
            self.runner.contexts(data)

    def test_old_run_requires_fresh_collection(self):
        self.store.save_run("old", "failed", {"snapshot": snapshot()})
        with self.store.db() as db:
            db.execute("UPDATE runs SET created=? WHERE id='old'", (time.time() - 8 * 86400,))
        with self.assertRaisesRegex(ValueError, "too old"):
            self.runner.run(resume_run_id="old")
        self.source.collect.assert_not_called()

    def test_weekly_analyst_cannot_introduce_product_launch(self):
        candidate = proposal("advisory")
        candidate["action"] = {"kind": "product_launch", "campaign_id": 1}
        with self.assertRaisesRegex(ValueError, "cannot create"):
            self.runner.validate_review({**review(), "proposals": [candidate]})


class ReadDiagnosticsTests(unittest.TestCase):
    def test_report_400_preserves_operation_and_error_but_redacts_secrets(self):
        direct = Mock()
        direct._build_reports_url.return_value = "https://api.direct.yandex.com/json/v5/reports"
        direct._build_reports_headers.return_value = {"Authorization": "Bearer secret-token"}
        payload = {"error": {"error_code": 8000, "error_string": "Invalid goal", "error_detail": "token=y0_secret-value", "request_id": "abc"}}
        error = urllib.error.HTTPError(direct._build_reports_url(), 400, "Bad Request", {}, io.BytesIO(json.dumps(payload).encode()))
        with patch("marketer.data.urllib.request.urlopen", side_effect=error) as request:
            with self.assertRaises(ReadError) as caught:
                DataSource(POLICY, direct=direct, metrika=Mock()).report(1, "2026-10-01", "2026-10-28", "CAMPAIGN_PERFORMANCE_REPORT", ["CampaignId"], 7, "AUTO")
        request.assert_called_once()
        self.assertEqual(caught.exception.diagnostic["request"]["campaign_id"], 1)
        self.assertEqual(caught.exception.diagnostic["details"]["error_code"], 8000)
        self.assertNotIn("secret-value", str(caught.exception))
        self.assertNotIn("secret-token", str(caught.exception))

    def test_redaction_and_non_json_errors(self):
        for secret in ("Bearer abc123", "access_token=abc123", "password='abc123'", '"botToken":"abc123"'):
            self.assertNotIn("abc123", safe_error(secret))
        self.assertEqual(ReadError("direct", "get", status=502, payload=[]).diagnostic["http_status"], 502)


if __name__ == "__main__":
    unittest.main()
