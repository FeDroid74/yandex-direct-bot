import copy
import json
import subprocess
import time
import uuid
from datetime import date, datetime, timedelta
from pathlib import Path

from marketer.data import sufficient, totals
from marketer.store import digest


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
            for key in ("queries", "ad_performance", "placements"):
                rows = c.get(key, [])
                c[key+"_total_rows"] = len(rows)
                # Send leading spend and converting rows; raw data remains in SQLite.
                ranked = sorted(rows, key=lambda r: ((r.get("Conversions") or 0) > 0, r.get("Cost", 0), r.get("Clicks", 0)), reverse=True)
                c[key] = [{k: v for k, v in r.items() if not k.startswith("Conversions_")} for r in ranked[:12]]
            c["ads_total_rows"] = len(c["ads"])
            ad_ids = {int(r["AdId"]) for r in c["ad_performance"][:10]}
            c["ads"] = sorted(c["ads"], key=lambda a: a["Id"] in ad_ids, reverse=True)[:6]
            for key in ("metrika_queries", "landing_pages"):
                if key in c:
                    c[key]["total_rows"] = len(c[key]["rows"])
                    c[key]["rows"] = c[key]["rows"][:8]
        history = [{"id": r["id"], "state": r["state"], "title": r["body"]["title"],
                    "campaign_id": r["body"]["campaign_id"], "action": r["body"]["action"]} for r in self.store.list(40)]
        compact["model_input_sampled"] = True
        for _ in range(5):
            result = {"snapshot": compact, "history": history}
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
        return result

    def evaluations(self):
        for row in self.store.list(500):
            if row["state"] != "applied" or row["evaluated_at"] or row["result"].get("already_present"):
                continue
            due = row["applied_at"] + (row["body"]["evaluate_after_days"]+self.policy["conversion_lag_days"])*86400
            if time.time() < due:
                continue
            e = row["body"]["evidence"]
            start = datetime.fromtimestamp(row["applied_at"]).date() + timedelta(days=1)
            end = date.today() - timedelta(days=self.policy["conversion_lag_days"]+1)
            rows = self.source.report(row["body"]["campaign_id"], start.isoformat(), end.isoformat(),
                                      "CAMPAIGN_PERFORMANCE_REPORT", ["Date", "CampaignId"], e["goal_id"], e["attribution"])
            after = totals(rows)
            if (after["Conversions"] < 5 or not after["ConversionsComplete"]) and time.time() < row["applied_at"]+90*86400:
                continue
            data = {"before": e["current"], "after": after, "date_from": start.isoformat(), "date_to": end.isoformat(),
                    "conclusion": "Наблюдение по кампании, не доказательство причинного эффекта. Учтите другие изменения и разную длину периодов."}
            self.telegram.notify(f"Оценка #{row['id']}: {row['body']['title']}\n"
                                 f"До: CPA {e['current']['CPA']}, конверсий {e['current']['Conversions']:g}.\n"
                                 f"После ({start} — {end}): CPA {after['CPA']}, конверсий {after['Conversions']:g}.\n"
                                 + data["conclusion"] + ("\nВыборка недостаточна или неполна; итог не определён." if after["Conversions"] < 5 or not after["ConversionsComplete"] else ""))
            self.store.evaluate(row["id"], data)

    def run(self, force=False, collect_only=False):
        last = self.store.setting("last_attempt", 0)
        if not force and time.time()-last < self.policy["min_run_interval_days"]*86400 - 300:
            return {"status": "not_due"}
        self.store.set_setting("last_attempt", time.time())
        run_id = uuid.uuid4().hex[:16]
        self.store.save_run(run_id, "running", {})
        try:
            snapshot = self.source.collect()
            snapshot["run_id"] = run_id
            self.store.set_setting("latest_snapshot", snapshot)
            allowed, reason = sufficient(snapshot, self.store.setting("last_analyzed_snapshot"), self.policy)
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
            self.store.save_run(run_id, "complete", {"snapshot": snapshot, "summary": analysis.get("summary"), "published": published, "rejected": rejected, "model_called": True})
            return {"status": "complete", "run_id": run_id, "published": published, "rejected": rejected}
        except Exception as exc:
            error = str(exc)[:1600]
            self.store.save_run(run_id, "failed", {"error": error})
            self.notify_once("run_error", "Автономный анализ не завершён. Кампании не менялись. Нужна проверка интеграции.\n" + error)
            raise
