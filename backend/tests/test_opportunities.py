import copy
import unittest
from unittest.mock import Mock
from marketer.opportunities import review, public_review
from marketer.runner import Runner
from marketer.billing import COUNTER, PURCHASE_GOAL
from services.direct_client import YandexDirectClient


class ReviewTests(unittest.TestCase):
    def setUp(self):
        self.cid, self.aid = 12, 1922465325980676323
        self.c = {"Id": self.cid, "Name": "Кампания", "State": "ON", "current": {"Clicks": 39},
                  "UnifiedCampaign": {"CounterIds": {"Items":[COUNTER]}, "BiddingStrategy":{
                      "Search":{"BiddingStrategyType":"PAY_FOR_CONVERSION","PayForConversion":{"GoalId": PURCHASE_GOAL}},
                      "Network":{"BiddingStrategyType":"SERVING_OFF"}}},
                  "ads":[{"Id":self.aid,"Status":"ACCEPTED","State":"ON","ShoppingAd":{
                      "DefaultTexts":["Военные статуэтки из фарфора."]}}]}
        self.sel = {"campaign_id":self.cid,"ad_id":self.aid,"status":"observed","available":20,
                    "composition":{"figurine_named":{"count":18},"tableware_named":{"count":2}}}
        self.s = {"run_id":"test","created":100,"campaigns":[self.c],"current_from":"2026-10-08",
                  "date_to":"2026-11-04","assortment":{"status":"ready","campaign_selections":[self.sel]}}

    def test_owner_only_candidate_links_exact_filter_and_ad(self):
        d = review(self.s, now=101)
        self.assertEqual(d["opportunities"][0]["action"]["ad_id"], self.aid)
        self.assertNotIn("action", public_review(d)["opportunities"][0])
        self.assertIn("18 статуэток", d["text"])
        self.assertEqual(len(d["opportunities"][0]["key"]), 20)

    def test_stale_wrong_filter_or_incomplete_composition_no_card(self):
        self.assertFalse(review(self.s, now=90000)["opportunities"])
        for patch in [{"ad_id":self.aid+1},{"campaign_id":13},{"available":35},{"status":"partial_catalog"}]:
            with self.subTest(patch=patch):
                data=copy.deepcopy(self.s)
                data["assortment"]["campaign_selections"][0].update(patch)
                self.assertFalse(review(data, now=101)["opportunities"])

    def test_mixed_goal_future_not_called_serving(self):
        self.c["UnifiedCampaign"]["BiddingStrategy"]["Search"]["BiddingStrategyType"]="PAY_FOR_CONVERSION_MULTIPLE_GOALS"
        self.c["StartDate"]="2030-11-27"
        d=review(self.s, now=101)
        self.assertFalse(d["opportunities"])
        self.assertEqual(d["future_billing_review"][0]["campaign_id"],12)

    def test_missing_network_placements_and_real_clicks_not_search_only(self):
        u=self.c["UnifiedCampaign"]
        u["BiddingStrategy"]["Network"]={"BiddingStrategyType":"NETWORK_DEFAULT"}
        u["BiddingStrategy"]["Search"]["PlacementTypes"]={"SearchResults":"YES"}
        self.assertEqual(YandexDirectClient.infer_unified_campaign_placement_type(u),"both")
        self.c["ad_performance"]=[{"AdNetworkType":"AD_NETWORK","Clicks":11}]
        self.sel["planned_placement"]="search_only"
        d=review(self.s,now=101)
        self.assertIn("11/39", d["text"])
        self.assertEqual(d["opportunities"][0]["action"]["kind"], "advisory")
        u["BiddingStrategy"]["Network"]={"BiddingStrategyType":"SERVING_OFF"}
        self.assertEqual(YandexDirectClient.infer_unified_campaign_placement_type(u),"search_only")

    def test_read_only_request_delivers_skipped_or_preview_without_applying(self):
        for state in ("skipped", "preview_ready"):
            with self.subTest(state=state):
                runner=Runner(Mock(),Mock(),Mock(),Mock(),{})
                runner.run=Mock(return_value={"run_id":"test"})
                runner.store.get_run.return_value={"id":"test","status":state,"data":{"snapshot":self.s}}
                runner.telegram.notify.return_value={"message_id":123}
                runner.requested_review()
                runner.run.assert_called_once_with(force=True,publish=False)
                runner.telegram.notify.assert_called_once()
                runner.actions.execute.assert_not_called()
                runner.store.set_setting.assert_called_with("requested_review_notice:test",{"delivery":"sent","message_id":123})

    def test_rejected_model_claim_is_not_announced_as_delivered(self):
        runner = Runner(Mock(), Mock(), Mock(), Mock(), {})
        runner.run = Mock(return_value={"run_id": "test"})
        runner.store.get_run.return_value = {"id": "test", "status": "preview_ready", "data": {
            "snapshot": self.s, "summary": "Обе карточки отправлены.", "prepared_count": 1,
            "rejected": [{"title": "Текст объявления", "error": "placement mismatch"}]}}
        runner.requested_review()
        message = runner.telegram.notify.call_args.args[0]
        self.assertIn("прошли проверку 1; не прошли 1", message)
        self.assertNotIn("Обе карточки отправлены", message)
        runner.actions.execute.assert_not_called()

    def test_invalid_model_output_delivers_same_run_facts_no_cards(self):
        runner=Runner(Mock(),Mock(),Mock(),Mock(),{})
        exc=ValueError("invalid JSON")
        exc.analysis_run_id="test"
        runner.run=Mock(side_effect=exc)
        runner.store.get_run.return_value={"id":"test","status":"failed","data":{"stage":"analysis","snapshot":self.s}}
        runner.telegram.notify.return_value={"message_id":125}
        runner.requested_review()
        runner.store.get_run.assert_called_once_with("test")
        self.assertIn("Модель не завершила",runner.telegram.notify.call_args.args[0])
        runner.actions.execute.assert_not_called()
