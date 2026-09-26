import copy
import json
import subprocess
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from marketer.data import sufficient, totals
from marketer.store import digest
from marketer.schedule import weekly_slot
from marketer.products import replacement_candidates


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

    def context(self, snapshot):
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
            for key in ("metrika_queries", "landing_pages"):
                if key in c:
                    c[key]["total_rows"] = len(c[key]["rows"])
                    c[key]["rows"] = [{k: v for k, v in r.items() if k != "dimension_key"} for r in c[key]["rows"][:8]]
        history = [{"id": r["id"], "state": r["state"], "title": r["body"]["title"],
                    "campaign_id": r["body"]["campaign_id"], "action": r["body"]["action"]} for r in self.store.list(40)]
        compact["model_input_sampled"] = True
        for _ in range(5):
            result = {"snapshot": compact, "history": history,
                      "product_replacement_review": replacement_candidates(snapshot),
                      "product_catalog": self.store.setting("product_catalog_last")}
            if len(json.dumps(result, ensure_ascii=False, separators=(",", ":")).encode()) <= 80000:
                return result
            for c in compact["campaigns"]:
                for key in ("queries", "ad_performance", "placements", "ads"):
                    c[key] = c.get(key, [])[:max(2, len(c.get(key, []))//2)]
                for key in ("metrika_queries", "landing_pages"):
                    if key in c:
                        c[key]["rows"] = c[key]["rows"][:max(2, len(c[key]["rows"])//2)]
        raise ValueError("Analysis input exceeds safe size; split campaign scope in policy")

    def model(self, snapshot):
        context = self.context(snapshot)
        prompt = Path(__file__).with_name("analyst_prompt.txt").read_text(encoding="utf-8")
        message = prompt + "\nДАННЫЕ:\n" + json.dumps(context, ensure_ascii=False, separators=(",", ":"))
        request_id = str(uuid.uuid4())
        params = {"agentId": self.policy["analyst_agent"], "message": message,
                  "sessionKey": f"agent:{self.policy['analyst_agent']}:analysis:{request_id}",
                  "idempotencyKey": request_id, "thinking": "medium", "timeout": 600, "deliver": False}
        completed = subprocess.run([self.policy["openclaw_command"], "gateway", "call", "agent", "--params",
                                    json.dumps(params, ensure_ascii=False), "--expect-final", "--timeout", "630000", "--json"],
                                   capture_output=True, text=True, timeout=660)
        if completed.returncode:
            if "OAuth token refresh failed" in completed.stderr:
                self.store.set_setting("analyst_auth", {"status": "reauthentication_required", "checked_at": time.time()})
                raise RuntimeError("OpenClaw требует повторного входа в ChatGPT: OAuth не обновляется. Доступ к Директу и Метрике от этого не зависит; рекламные изменения не выполнялись.")
            raise RuntimeError("OpenClaw analyst failed: " + completed.stderr[-1000:])
        output = completed.stdout
        # The CLI can prepend plugin registration logs before its JSON response.
        decoder = json.JSONDecoder()
        envelope = None
        for i, char in enumerate(output):
            if char != "{":
                continue
            try:
                value, _ = decoder.raw_decode(output[i:])
                if isinstance(value, dict) and ("result" in value or "payloads" in value):
                    envelope = value
                    break
            except ValueError:
                pass
        if envelope is None:
            raise ValueError("Missing OpenClaw JSON result")
        if envelope.get("result", envelope).get("meta", {}).get("stopReason") == "error":
            raise ValueError("OpenClaw model rejected the request; inspect model availability")
        payloads = envelope.get("result", envelope).get("payloads", [])
        answer = "\n".join(p.get("text", "") for p in payloads)
        result = json.loads(answer)
        if not isinstance(result, dict) or not isinstance(result.get("proposals"), list):
            raise ValueError("Invalid analyst output")
        if len(result["proposals"]) > self.policy["max_proposals_per_run"]:
            raise ValueError("Too many proposals; no cards published")
        coverage = result.get("coverage")
        if not isinstance(coverage, dict) or set(coverage) != AREAS:
            raise ValueError("Analyst must explicitly review all marketing areas")
        for area, review in coverage.items():
            if (not isinstance(review, dict) or review.get("status") not in ("proposal", "no_action", "insufficient_data") or
                    not isinstance(review.get("reason"), str) or not 1 <= len(review["reason"].strip()) <= 600):
                raise ValueError("Invalid marketing coverage: " + area)
        self.store.set_setting("analyst_auth", {"status": "ready", "checked_at": time.time()})
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

    def run(self, force=False, collect_only=False, scheduled=False):
        last = self.store.setting("last_attempt", 0)
        slot = weekly_slot(time.time(), self.policy.get("schedule_timezone", "Europe/Podgorica")) if scheduled else None
        if scheduled and (force or collect_only):
            raise ValueError("Scheduled analysis cannot be forced or collection-only")
        if scheduled and not self.store.claim_schedule(slot):
            return {"status": "already_scheduled", "schedule_slot": slot}
        if not scheduled and not force and time.time()-last < self.policy["min_run_interval_days"]*86400 - 300:
            return {"status": "not_due"}
        self.store.set_setting("last_attempt", time.time())
        run_id = uuid.uuid4().hex[:16]
        self.store.save_run(run_id, "running", {})
        try:
            snapshot = self.source.collect()
            snapshot["run_id"] = run_id
            snapshot["schedule_slot"] = slot
            self.store.set_setting("latest_snapshot", snapshot)
            catalog = self.store.setting("product_catalog_last")
            if isinstance(catalog, dict) and catalog.get("url"):
                try:
                    inspected = self.actions.products.catalog(catalog["url"])
                except Exception as exc:
                    self.store.set_setting("product_catalog_last", {"url": catalog["url"], "status": "unavailable",
                                                                   "checked_at": time.time(), "error": type(exc).__name__})
                    self.notify_once("product_feed_unavailable", "Не удалось обновить данные товарного фида. Актуальность ассортимента не подтверждена; кампании автоматически не изменялись.")
                else:
                    if inspected["invalid_count"] or inspected["unavailable"]:
                        self.notify_once("product_feed_invalid", "Товарная выгрузка требует проверки: в ней есть некорректные или отсутствующие товары. Проверьте настройки остатков и исключения товаров в InSales. Кампании автоматически не изменялись.")
            allowed, reason = sufficient(snapshot, self.store.setting("last_analyzed_snapshot"), self.policy)
            if slot in self.policy.get("required_review_slots", []) and sufficient(snapshot, None, self.policy)[0]:
                allowed, reason = True, "owner_requested_calendar_review"
            self.store.save_run(run_id, "collected", {"snapshot": snapshot, "gate": reason})
            if snapshot["errors"]:
                self.notify_once("data_errors", "Анализ Директа: часть данных недоступна. Для этих кампаний решения не формируются.\n" + json.dumps(snapshot["errors"], ensure_ascii=False)[:2500])
            if not collect_only:
                self.evaluations()
            if not allowed or collect_only:
                status = "collected_only" if collect_only else "skipped"
                self.store.save_run(run_id, status, {"snapshot": snapshot, "gate": reason, "model_called": False})
                return {"status": status, "gate": reason, "run_id": run_id, "campaigns": len(snapshot["campaigns"]), "errors": snapshot["errors"]}
            analysis = self.model(snapshot)
            published, rejected = [], []
            for raw in analysis["proposals"]:
                try:
                    body = self.actions.prepare(raw, snapshot)
                    row, fresh = self.store.create(body)
                    if fresh:
                        self.telegram.publish(row, self.store)
                        published.append(row["id"])
                except (ValueError, KeyError) as exc:
                    rejected.append({"title": str(raw.get("title", ""))[:180] if isinstance(raw, dict) else "invalid", "error": str(exc)[:500]})
            self.store.set_setting("last_analyzed_snapshot", snapshot)
            self.store.save_run(run_id, "complete", {"snapshot": snapshot, "summary": analysis.get("summary"), "coverage": analysis.get("coverage"),
                                                     "published": published, "rejected": rejected, "model_called": True})
            return {"status": "complete", "run_id": run_id, "published": published, "rejected": rejected}
        except Exception as exc:
            error = str(exc)[:1600]
            self.store.save_run(run_id, "failed", {"error": error})
            self.notify_once("run_error", "Автономный анализ не завершён. Кампании не менялись. Нужна проверка интеграции.\n" + error)
            raise
