import copy
import re
import threading
import time

from marketer.data import checked, number
from marketer.store import Conflict


AREAS = {"economics", "search", "rsya", "creative", "landing", "assortment", "experiment", "measurement"}


def text(value, name, maximum=1600):
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise ValueError(f"Invalid {name}")
    return value.strip()


def integer(value, name):
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"Invalid {name}")
    return value


def only(action, keys):
    if set(action) != set(keys):
        raise ValueError("Unexpected or missing action fields: " + str(sorted(set(action) ^ set(keys))))


def negative_items(group):
    return (group.get("NegativeKeywords") or {}).get("Items", [])


class Actions:
    def __init__(self, source, store, policy):
        self.source, self.store, self.policy = source, store, policy
        self.lock = threading.Lock()

    def prepare(self, raw, snapshot):
        if time.time() - snapshot.get("created", 0) > 7 * 86400:
            raise ValueError("Snapshot is too old; collect current data first")
        if not isinstance(raw, dict) or not isinstance(raw.get("action"), dict):
            raise ValueError("Proposal and action must be objects")
        cid = integer(raw.get("campaign_id"), "campaign_id")
        campaign = next((c for c in snapshot["campaigns"] if c["Id"] == cid), None)
        if not campaign:
            raise ValueError("Proposal must reference a complete collected campaign")
        area = raw.get("area")
        if area not in AREAS:
            raise ValueError("Unknown marketing area")
        action = copy.deepcopy(raw["action"])
        kind = action.get("kind")
        before, warnings = {}, []
        if kind == "add_negative":
            only(action, {"kind", "ad_group_id", "phrase"})
            gid = integer(action["ad_group_id"], "ad_group_id")
            phrase = text(action["phrase"], "phrase", 200)
            if not re.fullmatch(r"[\w\s-]+", phrase, flags=re.UNICODE) or len(phrase.split()) > 7:
                raise ValueError("Use an observed phrase of at most seven words without operators")
            group = next((g for g in campaign["groups"] if g["Id"] == gid), None)
            query_rows = [r for r in campaign["queries"] if str(r["AdGroupId"]) == str(gid) and r["Query"].casefold() == phrase.casefold()]
            if not group or not query_rows or any(number(r.get("Conversions")) != 0 or any(number(v) != 0 for v in r.get("conversions_by_goal", {}).values()) for r in query_rows):
                raise ValueError("Negative must be an observed, non-converting query in this group")
            if sum(r["Clicks"] for r in query_rows) < self.policy["min_negative_clicks"]:
                raise ValueError("Insufficient mature query clicks")
            if phrase.casefold() in {v.casefold() for v in negative_items(group)}:
                raise ValueError("Negative already exists")
            before = {"negative_keywords": negative_items(group)}
            warnings.append("Минус-фраза может сократить полезный охват. Проверьте смысл запроса и поздние конверсии.")
        elif kind == "ad_text":
            only(action, {"kind", "ad_id", "fields"})
            aid = integer(action["ad_id"], "ad_id")
            ad = next((a for a in campaign["ads"] if a["Id"] == aid), None)
            fields = action["fields"]
            if not ad or not ad.get("TextAd") or not isinstance(fields, dict) or not fields or set(fields) - {"Title", "Title2", "Text"}:
                raise ValueError("Only existing TextAd title/text fields can be updated")
            for key, value in fields.items():
                text(value, key, {"Title": 56, "Title2": 30, "Text": 81}[key])
            before = {key: ad["TextAd"].get(key) for key in fields}
            if all(before[k] == v for k, v in fields.items()):
                raise ValueError("No change")
            warnings.append("Изменённое объявление может пройти повторную модерацию. Эффект не гарантирован.")
        elif kind == "strategy_value":
            only(action, {"kind", "side", "field", "value_micros"})
            if action["side"] not in ("Search", "Network") or action["field"] not in ("Cpa", "WeeklySpendLimit"):
                raise ValueError("Only CPA or weekly budget of PAY_FOR_CONVERSION can be changed")
            unified = campaign["UnifiedCampaign"]
            side = unified["BiddingStrategy"].get(action["side"], {})
            if unified.get("PackageBiddingStrategy") or side.get("BiddingStrategyType") != "PAY_FOR_CONVERSION":
                raise ValueError("Unsupported or portfolio strategy: submit an advisory instead")
            current = side.get("PayForConversion", {}).get(action["field"])
            value = integer(action["value_micros"], "value_micros")
            if not current or value == current or abs(value/current-1) > self.policy["max_strategy_change_fraction"]:
                raise ValueError("Strategy change exceeds policy or has no effect")
            if not campaign["current"].get("ConversionsComplete", True) or campaign["current"]["Conversions"] < self.policy["min_strategy_conversions"]:
                raise ValueError("Insufficient conversions for a strategy change")
            before = {"value_micros": current, "goal_id": side["PayForConversion"]["GoalId"],
                      "type": side["BiddingStrategyType"]}
            warnings.append("Высокий риск: изменение CPA или бюджета может повлиять на обучение и расход. Гарантировать отсутствие переобучения нельзя.")
        elif kind == "advisory":
            only(action, {"kind", "task"})
            action["task"] = text(action["task"], "task", 1800)
            warnings.append("Ручной план: одобрение НЕ меняет Директ. Выполнение нужно отдельно отметить после фактической работы.")
        else:
            raise ValueError("Unsupported action kind")
        days = integer(raw.get("evaluate_after_days", 28), "evaluate_after_days")
        if not 14 <= days <= 90:
            raise ValueError("Evaluation window must be 14..90 days")
        return {"campaign_id": cid, "campaign_name": campaign["Name"], "area": area,
                "title": text(raw.get("title"), "title", 180),
                "reason": text(raw.get("reason"), "reason"),
                "expected_effect": text(raw.get("expected_effect"), "expected_effect", 800),
                "success_metric": text(raw.get("success_metric"), "success_metric", 800),
                "evaluate_after_days": days, "action": action, "before": before, "warnings": warnings,
                "evidence": {"run_id": snapshot["run_id"], "date_from": snapshot["current_from"],
                             "date_to": snapshot["date_to"], "goal_id": campaign["goal_id"],
                             "strategy_goals": campaign.get("strategy_goals", []),
                             "attribution": campaign["attribution"], "current": campaign["current"],
                             "previous": campaign["previous"], "limits": snapshot["limits"]}}

    def execute(self, row):
        body = row["body"]
        action, before, cid = body["action"], body["before"], body["campaign_id"]
        kind = action["kind"]
        direct = self.source.direct
        # Fetch fresh state, merge only the approved field, then re-read to verify.
        if kind == "add_negative":
            group = self.source.group(action["ad_group_id"])
            if group["CampaignId"] != cid:
                raise Conflict("Группа больше не соответствует кампании.")
            current = negative_items(group)
            if not set(before["negative_keywords"]).issubset(current):
                raise Conflict("Минус-фразы группы изменены извне; нужна новая карточка.")
            if action["phrase"] in current:
                return {"already_present": True, "before": current, "after": current}
            evidence = body["evidence"]
            rows = self.source.report(cid, evidence["date_from"], evidence["date_to"], "SEARCH_QUERY_PERFORMANCE_REPORT",
                                      ["CampaignId", "AdGroupId", "Query"], evidence["goal_id"], evidence["attribution"], evidence.get("strategy_goals"))
            relevant = [r for r in rows if str(r["AdGroupId"]) == str(action["ad_group_id"]) and r["Query"].casefold() == action["phrase"].casefold()]
            if not relevant or any(r["Conversions"] is None or r["Conversions"] > 0 or any(number(v) != 0 for v in r.get("conversions_by_goal", {}).values()) for r in relevant):
                raise Conflict("Появились конверсии либо данные запроса больше не подтверждены. Минусация отменена.")
            after = current + [action["phrase"]]
            response = direct.update_ad_group_negative_keywords(action["ad_group_id"], after)
            self.check_write(response, action["ad_group_id"])
            observed = negative_items(self.source.group(action["ad_group_id"]))
            if set(observed) != set(after):
                raise RuntimeError("Negative update result could not be verified")
            return {"before": current, "after": observed}
        if kind == "ad_text":
            ad = self.source.ad(action["ad_id"])
            if ad["CampaignId"] != cid or any(ad.get("TextAd", {}).get(k) != v for k, v in before.items()):
                raise Conflict("Поля объявления изменены. Нужна новая версия предложения.")
            fields = action["fields"]
            response = direct.update_text_ad_content(action["ad_id"], title=fields.get("Title"), title2=fields.get("Title2"), text=fields.get("Text"))
            self.check_write(response, action["ad_id"])
            observed = self.source.ad(action["ad_id"]).get("TextAd", {})
            if any(observed.get(k) != v for k, v in fields.items()):
                raise RuntimeError("Ad update could not be verified")
            return {"before": before, "after": fields}
        if kind == "strategy_value":
            campaign = self.source.campaign(cid)
            unified = campaign.get("UnifiedCampaign", {})
            strategy = copy.deepcopy(unified.get("BiddingStrategy", {}))
            side = strategy.get(action["side"], {})
            values = side.get("PayForConversion", {})
            if unified.get("PackageBiddingStrategy") or side.get("BiddingStrategyType") != before["type"] or values.get("GoalId") != before["goal_id"] or values.get(action["field"]) != before["value_micros"]:
                raise Conflict("Параметры стратегии изменились. Нужна новая карточка.")
            values[action["field"]] = action["value_micros"]
            response = direct.call_v501("campaigns", "update", {"Campaigns": [{"Id": cid, "UnifiedCampaign": {"BiddingStrategy": strategy}}]}).body
            self.check_write(response, cid)
            observed = self.source.campaign(cid)["UnifiedCampaign"]["BiddingStrategy"][action["side"]]["PayForConversion"][action["field"]]
            if observed != action["value_micros"]:
                raise RuntimeError("Strategy change could not be verified")
            return {"before": before["value_micros"], "after": observed}
        raise ValueError("Action is not executable")

    @staticmethod
    def check_write(response, entity_id):
        result = checked(response)
        items = result.get("UpdateResults", [])
        if len(items) != 1 or items[0].get("Errors") or items[0].get("Id") != entity_id:
            raise RuntimeError("Write failed or ambiguous: " + str(result)[:1000])

    def decide(self, pid, revision, decision, actor):
        with self.lock:
            row, changed = self.store.decide(pid, revision, decision, actor)
            if not changed or row["state"] != "applying":
                return row
            try:
                result = self.execute(row)
                return self.store.finish(pid, "applied", result)
            except Conflict as exc:
                return self.store.finish(pid, "stale", {"error": str(exc)})
            except Exception as exc:
                # Do not retry a write whose outcome may be unknown to us.
                return self.store.finish(pid, "uncertain", {"error": str(exc)[:1400], "retry_automatically": False})
