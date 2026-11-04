import copy
import json
import time
import uuid
from pathlib import Path

from marketer.data import sufficient
from marketer.store import digest
from marketer.schedule import weekly_slot
from marketer.products import replacement_candidates
from marketer.analyst import Analyst
from marketer.errors import safe_error
from marketer.memory import memory_context
from marketer.experiments import Experiments
from marketer.assortment import review as assortment_review, baseline_time
from marketer.knowledge import Knowledge
from marketer.autonomy import Autonomy
from marketer.telegram import summary, action_text


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

    def context(self, snapshot, bounded=True, decisions=None):
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
        campaign_ids = {c["Id"] for c in compact["campaigns"]}
        memory = decisions if decisions is not None else memory_context(self.store, sorted(campaign_ids))
        history = [r for r in memory["items"] if r["campaign_id"] in campaign_ids]
        total = sum(memory["counts"].get(cid, 0) for cid in campaign_ids)
        if isinstance(compact.get("assortment"), dict):
            compact["assortment"]["campaign_selections"] = [s for s in compact["assortment"].get("campaign_selections", []) if s["campaign_id"] in campaign_ids]
        compact["model_input_sampled"] = True
        for _ in range(5):
            result = {"snapshot": compact, "history": history, "decision_memory": {"total": total, "sampled": len(history) < total,
                                                                                 "full_history_tool": "marketer_memory", "paid_orders_available": False,
                                                                                 "shared_measurement_tasks": memory.get("shared_measurement_tasks", [])},
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
        decisions = memory_context(self.store, [c["Id"] for c in snapshot["campaigns"]])
        for campaign in snapshot["campaigns"]:
            trial = self.context({**snapshot, "campaigns": campaigns + [campaign]}, bounded=False, decisions=decisions)
            if campaigns and self.context_size(trial) > limit:
                batches.append(self.context({**snapshot, "campaigns": campaigns}, decisions=decisions))
                campaigns = []
            campaigns.append(campaign)
        if campaigns:
            batches.append(self.context({**snapshot, "campaigns": campaigns}, decisions=decisions))
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
        Experiments(self.store, self.source, self.telegram, self.policy).run()

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
            previous_snapshot = self.store.setting("last_analyzed_snapshot")
            if not resume_run_id:
                snapshot["assortment"] = assortment_review(self.store, snapshot["campaigns"], baseline_time(previous_snapshot))
                snapshot["knowledge"] = Knowledge(self.store).context(snapshot["campaigns"])
                self.store.set_setting("latest_snapshot", snapshot)
            allowed, reason = sufficient(snapshot, previous_snapshot, self.policy)
            changes = (snapshot.get("assortment") or {}).get("changes") or {}
            if reason == "insufficient_new_data" and any(change.get("count", 0) for change in changes.values()):
                allowed, reason = True, "observed_assortment_changed"
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
                    if self.policy.get("autonomy", {}).get("enabled"):
                        row = Autonomy(self.store, self.source, self.actions, self.policy).process(row, snapshot)
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
            if self.policy.get("autonomy", {}).get("enabled"):
                self.completion_notice(run_id, snapshot, analysis, published)
            return {"status": "complete", "run_id": run_id, "published": published, "rejected": rejected}
        except Exception as exc:
            error = safe_error(exc)
            self.store.save_run(run_id, "failed", {"error": error, "stage": stage})
            if publish:
                try:
                    self.notify_once("run_error", "Автономный анализ не завершён. Прогресс и результаты операций сохранены. Перед повтором требуется сверка; выполненные действия не повторяются автоматически.\n" + error)
                except Exception as notification_error:
                    self.store.save_run(run_id, "failed", {"notification_error": safe_error(notification_error)})
            raise

    def completion_notice(self, run_id, snapshot, analysis, published):
        key = "completion_notice:"+run_id
        if self.store.setting(key):
            return
        rows = [self.store.get(pid) for pid in published]
        applied = [r for r in rows if r["state"] == "applied" and (r.get("result") or {}).get("authorization", {}).get("mode") == "autonomous"]
        pending = [r for r in rows if r["state"] == "pending"]
        uncertain = [r for r in rows if r["state"] == "uncertain"]
        parts = [f"Анализ {snapshot['current_from']} — {snapshot['date_to']}: {len(snapshot['campaigns'])} кампаний."]
        if applied:
            parts.append("Автоматически применено и проверено:\n"+"\n".join(
                f"• {summary(r['body']['campaign_name'], 150)}: {summary(action_text(r['body']), 450)}" for r in applied))
        elif uncertain:
            parts.append("Подтверждённых автоматических изменений нет; результат попытки требуется сверить.")
        else:
            parts.append("Автоматических изменений нет: подходящих проверенных действий не было.")
        if pending:
            parts.append(f"На отдельное решение оставлено: {len(pending)}. Карточки отправлены отдельно.")
        if uncertain:
            parts.append("Есть операция с неизвестным результатом. Автозаписи остановлены до сверки; повтора не будет.")
        parts.append(summary(analysis.get("summary", ""), 1500))
        parts.append("Автоматический режим не меняет цель покупки и способ оплаты. Достижение цели не подтверждает оплату заказа.")
        message = summary("\n\n".join(parts), 3900)
        self.store.set_setting(key, {"delivery": "uncertain", "at": time.time()})
        try:
            sent = self.telegram.notify(message)
            self.store.set_setting(key, {"delivery": "sent", "message_id": (sent or {}).get("message_id"), "at": time.time()})
        except Exception as exc:
            self.store.set_setting(key, {"delivery": "uncertain", "error": safe_error(exc), "at": time.time()})
