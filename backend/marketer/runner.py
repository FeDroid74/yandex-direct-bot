import copy
import json
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from marketer.data import sufficient, totals
from marketer.store import digest
from marketer.schedule import weekly_slot
from marketer.products import replacement_candidates
from marketer.analyst import Analyst
from marketer.errors import safe_error


AREAS = {"economics", "search", "rsya", "creative", "landing", "assortment", "experiment", "measurement"}


def sample_rows(rows, limit=12):
    selected = []
    scores = [max([r.get("Conversions") or 0] + [v or 0 for v in r.get("conversions_by_goal", {}).values()]) for r in rows]
    converting = sorted((i for i in range(len(rows)) if scores[i] > 0), key=lambda i: scores[i], reverse=True)
    for ranked in (converting[:limit//3], sorted(range(len(rows)), key=lambda i: rows[i].get("Clicks", 0), reverse=True)[:limit//3],
                   sorted(range(len(rows)), key=lambda i: (rows[i].get("Cost", 0), rows[i].get("Clicks", 0)), reverse=True)):
        for index in ranked:
            if index not in selected:
                selected.append(index)
            if len(selected) == limit:
                return [rows[i] for i in selected]
    return [rows[i] for i in selected]


class Runner:
    def __init__(self, store, source, actions, telegram, policy):
        self.store, self.source, self.actions, self.telegram, self.policy = store, source, actions, telegram, policy

    def notify_once(self, kind, message):
        signature = digest(message)
        if self.store.setting("notification:" + kind) != signature:
            self.telegram.notify(message)
            self.store.set_setting("notification:" + kind, signature)

    def context(self, snapshot, bounded=True):
        if not snapshot:
            return {"snapshot": None, "history": []}
        compact = copy.deepcopy(snapshot)
        for c in compact["campaigns"]:
            c.pop("daily", None)
            c.pop("period_reports", None)
            diagnosis = c.get("diagnostics", {})
            diagnosis.pop("limits", None)
            diagnosis.pop("independent_analysis", None)
            for goal in diagnosis.get("goals", {}).values():
                for key in ("direct", "metrika", "counter_all_traffic"):
                    goal.pop(key, None)
            for group in c["groups"]:
                group.pop("CampaignId", None)
                negatives = (group.pop("NegativeKeywords", None) or {}).get("Items", [])
                group["negative_keywords_count"] = len(negatives)
                group["negative_keywords_sample"] = negatives[:5]
            for key in ("queries", "ad_performance", "placements"):
                rows = c.get(key, [])
                c[key+"_total_rows"] = len(rows)
                # Include high-click rows even when conversion billing leaves their cost at zero.
                c[key] = [{k: v for k, v in r.items() if not k.startswith("Conversions_") and k not in ("raw_conversions_by_goal", "CampaignId")} for r in sample_rows(rows)]
            c["ads_total_rows"] = len(c["ads"])
            ad_ids = {int(r["AdId"]) for r in c["ad_performance"][:10]}
            c["ads"] = sorted(c["ads"], key=lambda a: a["Id"] in ad_ids, reverse=True)[:6]
            for ad in c["ads"]:
                ad.pop("CampaignId", None)
            c["groups_total_rows"] = len(c["groups"])
            relevant_groups = {str(r.get("AdGroupId")) for r in c["queries"] + c["ad_performance"] + c["ads"]}
            c["groups"] = sorted(c["groups"], key=lambda g: str(g["Id"]) in relevant_groups, reverse=True)[:60]
            for key in ("metrika_queries", "landing_pages"):
                if key in c:
                    c[key]["total_rows"] = len(c[key]["rows"])
                    c[key]["rows"] = [{k: v for k, v in r.items() if k != "dimension_key"} for r in c[key]["rows"][:8]]
        history = [{"id": r["id"], "state": r["state"], "title": r["body"]["title"],
                    "campaign_id": r["body"]["campaign_id"], "action": r["body"]["action"]} for r in self.store.list(40)
                   if r["body"]["campaign_id"] in {c["Id"] for c in compact["campaigns"]}]
        compact["model_input_sampled"] = True
        for _ in range(5):
            result = {"snapshot": compact, "history": history,
                      "product_replacement_review": replacement_candidates(snapshot),
                      "product_catalog": self.store.setting("product_catalog_last")}
            if not bounded or self.context_size(result) <= self.policy.get("model_context_max_bytes", 70000):
                return result
            for c in compact["campaigns"]:
                for key in ("queries", "ad_performance", "placements", "ads"):
                    c[key] = c.get(key, [])[:max(2, len(c.get(key, []))//2)]
                for key in ("metrika_queries", "landing_pages"):
                    if key in c:
                        c[key]["rows"] = c[key]["rows"][:max(2, len(c[key]["rows"])//2)]
        raise ValueError("One analysis batch exceeds safe size; inspect campaign metadata, no campaigns were silently discarded")

    @staticmethod
    def context_size(value):
        return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode())

    def contexts(self, snapshot):
        batches, campaigns = [], []
        limit = self.policy.get("model_context_max_bytes", 70000)
        for campaign in snapshot["campaigns"]:
            trial = self.context({**snapshot, "campaigns": campaigns + [campaign]}, bounded=False)
            if campaigns and self.context_size(trial) > limit:
                batches.append(self.context({**snapshot, "campaigns": campaigns}))
                campaigns = []
            campaigns.append(campaign)
        if campaigns:
            batches.append(self.context({**snapshot, "campaigns": campaigns}))
        for index, batch in enumerate(batches):
            batch["scope"] = {"batch": index + 1, "batches": len(batches), "account_campaign_count": len(snapshot["campaigns"]),
                              "campaign_ids": [c["Id"] for c in batch["snapshot"]["campaigns"]]}
        return batches

    def context_page(self, snapshot, batch=None):
        if not snapshot:
            return {"snapshot": None, "history": []}
        contexts = self.contexts(snapshot)
        if batch is not None:
            if type(batch) is not int or not 1 <= batch <= len(contexts):
                raise ValueError("Invalid context batch")
            return contexts[batch - 1]
        if len(contexts) == 1:
            return contexts[0]
        return {"overview_only": True, "run_id": snapshot.get("run_id"),
                "batches": [c["scope"] for c in contexts],
                "message": "Read each context batch; the account exceeds one bounded model input."}

    def model(self, snapshot):
        run_id = snapshot.get("run_id") or uuid.uuid4().hex[:16]
        plan_key = "analysis_plan:" + run_id
        plan = self.store.setting(plan_key)
        if plan is None:
            plan = self.contexts(snapshot)
            self.store.set_setting(plan_key, plan)
        if not plan:
            raise ValueError("No complete campaigns to analyze")
        prompt = Path(__file__).with_name("analyst_prompt.txt").read_text(encoding="utf-8")
        analyst = Analyst(self.store, self.policy)
        results = []
        for index, context in enumerate(plan):
            scope = set(context["scope"]["campaign_ids"])
            def validate(result, scope=scope):
                self.validate_review(result)
                if any(p.get("campaign_id") not in scope for p in result["proposals"]):
                    raise ValueError("Proposal references a campaign outside its analysis batch")
                return result
            results.append(analyst.call(run_id, f"batch-{index + 1:03d}", context, prompt, validate))
        if len(results) == 1:
            return results[0]
        # Hierarchical selection keeps every batch in the review without growing one giant prompt.
        merge_prompt = prompt + "\nЭто итоговый отбор по результатам частей одного снимка. Не складывай повторяющиеся показатели. " \
            "Не создавай и не переписывай действия. Верни JSON с summary, coverage всех восьми областей и " \
            "selected_ids: массив ID выбранных кандидатов (не более 8). Учитывай все обзоры, не только первую кампанию."
        level = 0
        while len(results) > 1:
            merged = []
            for offset in range(0, len(results), 2):
                parts = results[offset:offset+2]
                if len(parts) == 1:
                    merged.append(parts[0])
                    continue
                candidates = {}
                for part in parts:
                    for candidate in part["proposals"]:
                        candidates.setdefault(digest(candidate), candidate)
                context = {"date_from": snapshot["current_from"], "date_to": snapshot["date_to"],
                           "account_overview": [{"campaign_id": c["Id"], "name": c["Name"], "goal_id": c["goal_id"],
                                                 "attribution": c["attribution"],
                                                 "current": {k: c["current"].get(k) for k in
                                                             ("Clicks", "Cost", "Conversions", "ConversionsComplete", "CPA")}}
                                                for c in snapshot["campaigns"]],
                           "reviews": [{"summary": p.get("summary"), "coverage": p["coverage"]} for p in parts],
                           "candidates": [{"id": k, **v} for k, v in candidates.items()]}
                def select(result, candidates=candidates):
                    ids = result.get("selected_ids") if isinstance(result, dict) else None
                    if not isinstance(ids, list) or any(not isinstance(k, str) or k not in candidates for k in ids) or len(ids) != len(set(ids)):
                        raise ValueError("Invalid portfolio selection; actions cannot be invented")
                    self.validate_review({**result, "proposals": [candidates[k] for k in ids]})
                    return result
                choice = analyst.call(run_id, f"merge-{level:02d}-{offset:03d}", context, merge_prompt, select)
                merged.append({"summary": choice.get("summary"), "coverage": choice["coverage"],
                               "proposals": [candidates[k] for k in choice["selected_ids"]]})
            results, level = merged, level + 1
        return results[0]

    def validate_review(self, result):
        if not isinstance(result, dict) or not isinstance(result.get("proposals"), list):
            raise ValueError("Invalid analyst output")
        if any(not isinstance(p, dict) for p in result["proposals"]):
            raise ValueError("Invalid proposal object")
        if any(not isinstance(p.get("action"), dict) or p["action"].get("kind") not in
               {"add_negative", "ad_text", "strategy_value", "advisory"} for p in result["proposals"]):
            raise ValueError("Weekly analysis cannot create, launch, or stop campaigns")
        if not isinstance(result.get("summary"), str) or len(result["summary"]) > 2500:
            raise ValueError("Invalid analyst summary")
        if self.context_size(result) > 35000:
            raise ValueError("Analyst output exceeds safe size; no cards published")
        if len(result["proposals"]) > self.policy["max_proposals_per_run"]:
            raise ValueError("Too many proposals; no cards published")
        coverage = result.get("coverage")
        if not isinstance(coverage, dict) or set(coverage) != AREAS:
            raise ValueError("Analyst must explicitly review all marketing areas")
        for area, review in coverage.items():
            if (not isinstance(review, dict) or review.get("status") not in ("proposal", "no_action", "insufficient_data") or
                    not isinstance(review.get("reason"), str) or not 1 <= len(review["reason"].strip()) <= 600):
                raise ValueError("Invalid marketing coverage: " + area)
        return result

    def evaluations(self):
        for row in self.store.list(500):
            if row["state"] != "applied" or row["evaluated_at"] or row["result"].get("already_present"):
                continue
            if row["body"]["evidence"].get("product_workflow") and row["body"]["action"]["kind"] != "product_launch":
                self.store.evaluate(row["id"], {"operational_only": True, "result": row["result"],
                                               "conclusion": "Настройка не является оценкой продаж; пилот оценивается после запуска."})
                continue
            due = row["applied_at"] + (row["body"]["evaluate_after_days"]+self.policy["conversion_lag_days"])*86400
            if time.time() < due:
                continue
            e = row["body"]["evidence"]
            report_timezone = ZoneInfo(self.policy.get("report_timezone", "Europe/Moscow"))
            start = datetime.fromtimestamp(row["applied_at"], report_timezone).date() + timedelta(days=1)
            end = datetime.now(report_timezone).date() - timedelta(days=self.policy["conversion_lag_days"]+1)
            if e.get("product_workflow"):
                cid = row["body"]["campaign_id"]
                campaign = self.source.campaign(cid)
                attribution = campaign.get("UnifiedCampaign", {}).get("AttributionModel", "AUTO")
                rows = self.source.report(cid, start.isoformat(), end.isoformat(), "CAMPAIGN_PERFORMANCE_REPORT",
                                          ["CampaignId"], e["goal_id"], attribution, [e["goal_id"]])
                after = totals(rows, [e["goal_id"]])
                count = f"{after['Conversions']:g}" if after["ConversionsComplete"] else "неизвестно, часть данных недоступна"
                data = {"after": after, "date_from": start.isoformat(), "date_to": end.isoformat(), "attribution": attribution,
                        "conclusion": "Ecommerce-покупки не подтверждают оплату. Это наблюдение после запуска, не доказательство прироста относительно старой кампании."}
                self.telegram.notify(f"Товарный пилот {cid}, оценка #{row['id']} ({start} — {end}).\n"
                                     f"Клики: {after['Clicks']:g}; расход: {after['Cost']:g} руб.; покупки (целевые визиты Директа): {count}.\n" + data["conclusion"])
                self.store.evaluate(row["id"], data)
                continue
            rows = self.source.report(row["body"]["campaign_id"], start.isoformat(), end.isoformat(),
                                      "CAMPAIGN_PERFORMANCE_REPORT", ["CampaignId"], e["goal_id"], e["attribution"], e.get("strategy_goals"))
            after = totals(rows, e.get("strategy_goals", []))
            if (after["Conversions"] < 5 or not after["ConversionsComplete"]) and time.time() < row["applied_at"]+90*86400:
                continue
            data = {"before": e["current"], "after": after, "date_from": start.isoformat(), "date_to": end.isoformat(),
                    "conclusion": "Наблюдение по кампании, не доказательство причинного эффекта. Учтите другие изменения и разную длину периодов."}
            self.telegram.notify(f"Оценка #{row['id']}: {row['body']['title']}\n"
                                 f"До: CPA {e['current']['CPA']}, конверсий {e['current']['Conversions']:g}.\n"
                                 f"После ({start} — {end}): CPA {after['CPA']}, конверсий {after['Conversions']:g}.\n"
                                 + data["conclusion"] + ("\nВыборка недостаточна или неполна; итог не определён." if after["Conversions"] < 5 or not after["ConversionsComplete"] else ""))
            self.store.evaluate(row["id"], data)

    def run(self, force=False, collect_only=False, scheduled=False, resume_run_id=None, publish=True):
        last = self.store.setting("last_attempt", 0)
        slot = weekly_slot(time.time(), self.policy.get("schedule_timezone", "Europe/Podgorica")) if scheduled else None
        if scheduled and (force or collect_only or resume_run_id or not publish):
            raise ValueError("Scheduled analysis cannot be forced or collection-only")
        if resume_run_id and (force or collect_only):
            raise ValueError("Resume cannot be combined with force or collection-only")
        if scheduled and not self.store.claim_schedule(slot):
            return {"status": "already_scheduled", "schedule_slot": slot}
        if not resume_run_id and not scheduled and not force and time.time()-last < self.policy["min_run_interval_days"]*86400 - 300:
            return {"status": "not_due"}
        if resume_run_id:
            prior = self.store.get_run(resume_run_id)
            if prior["status"] in ("complete", "skipped", "collected_only"):
                raise ValueError("This run does not need recovery")
            if time.time() - prior["created"] > 7 * 86400:
                raise ValueError("Run is too old to resume; collect fresh statistics")
            run_id, data = resume_run_id, prior["data"]
        else:
            self.store.set_setting("last_attempt", time.time())
            run_id, data = uuid.uuid4().hex[:16], {}
        stage = "collection"
        self.store.save_run(run_id, "running", {"stage": stage, "error": None, "publish_requested": publish})
        try:
            snapshot = data.get("snapshot") or self.source.collect()
            snapshot["run_id"] = run_id
            if not resume_run_id:
                snapshot["schedule_slot"] = slot
            self.store.set_setting("latest_snapshot", snapshot)
            self.store.save_run(run_id, "collected", {"snapshot": snapshot, "stage": "data_gate"})
            catalog = self.store.setting("product_catalog_last")
            if not resume_run_id and isinstance(catalog, dict) and catalog.get("url"):
                try:
                    inspected = self.actions.products.catalog(catalog["url"])
                except Exception as exc:
                    self.store.set_setting("product_catalog_last", {"url": catalog["url"], "status": "unavailable",
                                                                   "checked_at": time.time(), "error": type(exc).__name__})
                    if publish:
                        self.notify_once("product_feed_unavailable", "Не удалось обновить данные товарного фида. Актуальность ассортимента не подтверждена; кампании автоматически не изменялись.")
                else:
                    if publish and (inspected["invalid_count"] or inspected["unavailable"]):
                        self.notify_once("product_feed_invalid", "Товарная выгрузка требует проверки: в ней есть некорректные или отсутствующие товары. Проверьте настройки остатков и исключения товаров в InSales. Кампании автоматически не изменялись.")
            allowed, reason = sufficient(snapshot, self.store.setting("last_analyzed_snapshot"), self.policy)
            if resume_run_id and data.get("gate_allowed"):
                allowed, reason = True, data.get("gate", "resuming_saved_analysis")
            if slot in self.policy.get("required_review_slots", []) and sufficient(snapshot, None, self.policy)[0]:
                allowed, reason = True, "owner_requested_calendar_review"
            self.store.save_run(run_id, "collected", {"snapshot": snapshot, "gate": reason, "gate_allowed": allowed})
            if publish and snapshot["errors"]:
                self.notify_once("data_errors", "Анализ Директа: часть данных недоступна. Для этих кампаний решения не формируются.\n" + json.dumps(snapshot["errors"], ensure_ascii=False)[:2500])
            if not collect_only and publish and not data.get("evaluations_checked"):
                try:
                    self.evaluations()
                    self.store.save_run(run_id, "collected", {"evaluations_checked": True})
                except Exception as exc:
                    self.store.save_run(run_id, "collected", {"evaluation_error": safe_error(exc)})
                    self.notify_once("evaluation_error", "Не завершена оценка прежних изменений. Новый анализ продолжается; причина сохранена в журнале.")
            if not allowed or collect_only:
                status = "collected_only" if collect_only else "skipped"
                self.store.save_run(run_id, status, {"snapshot": snapshot, "gate": reason, "model_called": False, "stage": "finished"})
                return {"status": status, "gate": reason, "run_id": run_id, "campaigns": len(snapshot["campaigns"]), "errors": snapshot["errors"]}
            stage = "analysis"
            self.store.save_run(run_id, "analyzing", {"stage": stage, "model_called": True})
            analysis = data.get("analysis") or self.model(snapshot)
            self.store.save_run(run_id, "analyzing", {"analysis": analysis, "summary": analysis.get("summary"), "coverage": analysis.get("coverage")})
            published, rejected, prepared = list(data.get("published", [])), [], []
            for raw in analysis["proposals"]:
                try:
                    body = self.actions.prepare(raw, snapshot)
                    prepared.append(body)
                except (ValueError, KeyError) as exc:
                    rejected.append({"title": str(raw.get("title", ""))[:180] if isinstance(raw, dict) else "invalid", "error": safe_error(exc, 500)})
            if not publish:
                self.store.save_run(run_id, "preview_ready", {"stage": "preview", "rejected": rejected, "prepared_count": len(prepared)})
                return {"status": "preview_ready", "run_id": run_id, "prepared_count": len(prepared), "rejected": rejected}
            stage = "publication"
            self.store.save_run(run_id, "publishing", {"stage": stage, "rejected": rejected})
            delivery_issues = []
            for body in prepared:
                row, fresh = self.store.create(body)
                if fresh or row["body"]["evidence"].get("run_id") == run_id:
                    if row["delivery"] == "pending":
                        self.telegram.publish(row, self.store)
                        row = self.store.get(row["id"])
                    if row["delivery"] == "sent":
                        if row["id"] not in published:
                            published.append(row["id"])
                    else:
                        delivery_issues.append({"id": row["id"], "delivery": row["delivery"]})
                    self.store.save_run(run_id, "publishing", {"published": published, "delivery_issues": delivery_issues})
            if delivery_issues:
                self.store.save_run(run_id, "delivery_uncertain", {"stage": stage, "delivery_issues": delivery_issues})
                return {"status": "delivery_uncertain", "run_id": run_id, "published": published, "delivery_issues": delivery_issues}
            self.store.set_setting("last_analyzed_snapshot", snapshot)
            self.store.save_run(run_id, "complete", {"snapshot": snapshot, "summary": analysis.get("summary"), "coverage": analysis.get("coverage"),
                                                     "published": published, "rejected": rejected, "model_called": True,
                                                     "stage": "finished", "completed_at": time.time(), "error": None})
            return {"status": "complete", "run_id": run_id, "published": published, "rejected": rejected}
        except Exception as exc:
            error = safe_error(exc)
            self.store.save_run(run_id, "failed", {"error": error, "stage": stage})
            if publish:
                try:
                    self.notify_once("run_error", "Автономный анализ не завершён. Кампании не менялись. Прогресс сохранён.\n" + error)
                except Exception as notification_error:
                    self.store.save_run(run_id, "failed", {"notification_error": safe_error(notification_error)})
            raise
