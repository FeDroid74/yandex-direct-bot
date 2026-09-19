import csv
import hashlib
import io
import json
import math
import time
import urllib.request
from datetime import date, timedelta

from services.direct_client import YandexDirectClient, YandexDirectClientError
from services.metrika_client import YandexMetrikaClient


def number(value):
    if isinstance(value, bool) or value is None:
        return None
    try:
        result = float(str(value).replace(",", "."))
        return result if math.isfinite(result) else None
    except (ValueError, TypeError):
        return None


def checked(body):
    if not isinstance(body, dict) or body.get("error") or not isinstance(body.get("result"), dict):
        raise YandexDirectClientError("Direct API error: " + json.dumps(body, ensure_ascii=False)[:1200])
    return body["result"]


class DataSource:
    def __init__(self, policy, direct=None, metrika=None):
        self.policy = policy
        self.direct = direct or YandexDirectClient.for_target("production")
        self.metrika = metrika or YandexMetrikaClient()

    def entities(self, service, key, params):
        rows, offset = [], 0
        for _ in range(200):
            response = self.direct.call_v501(service, "get", {**params, "Page": {"Limit": 1000, "Offset": offset}})
            result = checked(response.body)
            page = result.get(key)
            if not isinstance(page, list):
                raise ValueError("Missing entity list: " + key)
            rows.extend(page)
            next_offset = result.get("LimitedBy")
            if next_offset is None:
                return rows
            if not page or int(next_offset) <= offset:
                raise ValueError("Non-progressing Direct pagination")
            offset = int(next_offset)
        raise ValueError("Direct pagination safety limit reached; data incomplete")

    def campaign(self, cid):
        rows = self.entities("campaigns", "Campaigns", {
            "SelectionCriteria": {"Ids": [cid]},
            "FieldNames": ["Id", "Name", "State", "Status", "Type", "StartDate"],
            "UnifiedCampaignFieldNames": ["BiddingStrategy", "CounterIds", "AttributionModel", "PackageBiddingStrategy", "PriorityGoals"],
            "UnifiedCampaignSearchStrategyPlacementTypesFieldNames": ["SearchResults", "ProductGallery", "DynamicPlaces", "Maps", "SearchOrganizationList"],
            "UnifiedCampaignNetworkStrategyPlacementTypesFieldNames": ["Network", "Maps"],
        })
        if len(rows) != 1:
            raise ValueError("Campaign not found")
        return rows[0]

    def group(self, gid):
        rows = self.entities("adgroups", "AdGroups", {
            "SelectionCriteria": {"Ids": [gid]},
            "FieldNames": ["Id", "CampaignId", "Name", "NegativeKeywords", "Status"],
        })
        if len(rows) != 1:
            raise ValueError("Ad group not found")
        return rows[0]

    def ad(self, aid):
        result = checked(self.direct.get_ad_details(aid))
        if len(result.get("Ads", [])) != 1:
            raise ValueError("Ad not found")
        return result["Ads"][0]

    def report(self, cid, start, end, kind, dimensions, goal_id, attribution, extra_goals=None):
        goals = list(dict.fromkeys([goal_id] + list(extra_goals or [])))
        variant = hashlib.sha256(json.dumps([sorted(goals), dimensions]).encode()).hexdigest()[:8]
        params = {
            "SelectionCriteria": {"DateFrom": start, "DateTo": end,
                                  "Filter": [{"Field": "CampaignId", "Operator": "IN", "Values": [str(cid)]}]},
            "FieldNames": dimensions + ["Impressions", "Clicks", "Cost", "Conversions"],
            "Goals": [str(g) for g in goals], "AttributionModels": [attribution],
            "ReportName": f"marketer-{cid}-{kind}-{start}-{end}-{goal_id}-{attribution}-{variant}",
            "ReportType": kind, "DateRangeType": "CUSTOM_DATE", "Format": "TSV",
            "IncludeVAT": "YES", "IncludeDiscount": "YES",
        }
        for attempt in range(5):
            req = urllib.request.Request(self.direct._build_reports_url(),
                                         data=json.dumps({"params": params}).encode(),
                                         headers=self.direct._build_reports_headers(), method="POST")
            with urllib.request.urlopen(req, timeout=90) as response:
                if response.status in (201, 202):
                    delay = min(60, max(1, int(response.headers.get("retryIn", "5"))))
                    if attempt == 4:
                        raise ValueError("Direct report still processing; no analysis on partial data")
                    time.sleep(delay)
                    continue
                raw = response.read(32 * 1024 * 1024)
                if len(raw) >= 32 * 1024 * 1024:
                    raise ValueError("Report exceeds size limit")
                reader = csv.DictReader(io.StringIO(raw.decode()), delimiter="\t")
                required = set(dimensions + ["Impressions", "Clicks", "Cost"] + [f"Conversions_{g}_{attribution}" for g in goals])
                if not required.issubset(reader.fieldnames or []):
                    raise ValueError("Invalid Direct TSV header; report is not complete")
                rows = list(reader)
            for row in rows:
                conversion = row.get(f"Conversions_{goal_id}_{attribution}")
                if conversion is None:
                    raise ValueError("Report missing selected-goal conversions")
                row["Conversions"] = number(conversion)
                if any(f"Conversions_{g}_{attribution}" not in row for g in goals):
                    raise ValueError("Report missing one of the selected strategy goals")
                row["conversions_by_goal"] = {str(g): number(row.get(f"Conversions_{g}_{attribution}")) for g in goals}
                for name in ("Clicks", "Cost", "Impressions"):
                    row[name] = number(row.get(name))
                if any(row[k] is None for k in ("Clicks", "Cost", "Impressions")):
                    raise ValueError("Unknown report metric; not equivalent to zero")
            return rows
        raise ValueError("Report not ready")

    def metrika_report(self, cid, start, end, goal_id, dimension):
        metrics = ["ym:s:visits", "ym:s:users", "ym:s:bounceRate", "ym:s:pageDepth",
                   "ym:s:avgVisitDurationSeconds", f"ym:s:goal{goal_id}reaches"]
        params = {"ids": str(self.policy["counter_id"]), "date1": start, "date2": end,
                  "metrics": ",".join(metrics), "dimensions": dimension,
                  "filters": f"ym:s:lastsignDirectClickOrder=='{cid}'", "accuracy": "full",
                  "limit": 1000, "lang": "ru", "sort": "-ym:s:visits"}
        rows = []
        for _ in range(100):
            result = self.metrika._get_json("/stat/v1/data", {**params, "offset": len(rows)+1})
            if result.get("sampled") or result.get("total_rows_rounded"):
                raise ValueError("Metrika returned sampled/rounded data")
            page = result.get("data")
            if not isinstance(page, list) or "total_rows" not in result:
                raise ValueError("Invalid Metrika report")
            for entry in page:
                values = entry.get("metrics", [])
                if len(values) != len(metrics) or any(number(v) is None for v in values):
                    raise ValueError("Unknown Metrika metric")
                rows.append({"label": entry["dimensions"][0].get("name"),
                             **dict(zip(["visits", "users", "bounce_rate", "page_depth", "duration", "goal_reaches"], values))})
            if len(rows) >= int(result["total_rows"]):
                return {"rows": rows, "totals": result.get("totals"), "attribution": "lastsign",
                        "dimension": dimension, "sampled": False}
            if not page:
                raise ValueError("Incomplete Metrika pagination")
        raise ValueError("Metrika pagination limit reached")

    def collect(self, today=None):
        today = today or date.today()
        end = today - timedelta(days=self.policy["conversion_lag_days"]+1)
        start = end - timedelta(days=self.policy["lookback_days"]*2-1)
        split = end - timedelta(days=self.policy["lookback_days"]-1)
        campaigns = self.entities("campaigns", "Campaigns", {
            "SelectionCriteria": {"States": ["ON", "SUSPENDED"]},
            "FieldNames": ["Id", "Name", "State", "Type"],
        })
        result = {"date_from": start.isoformat(), "date_to": end.isoformat(), "current_from": split.isoformat(),
                  "created": time.time(), "campaigns": [], "errors": [],
                  "limits": ["Достижения целей не равны оплаченным заказам; нет CRM, маржи и возвратов.",
                             "Прочерки в конверсиях Директа сохранены как неизвестные, не как нули. Сумма включает только числовые значения; при прочерках CPA не вычисляется.",
                             "Метрика: lastsign, Директ: атрибуция кампании. Их числа нельзя складывать.",
                             "Недавние дни исключены для дозревания конверсий; это не гарантирует полноту поздних конверсий."]}
        for basic in campaigns:
            cid = basic["Id"]
            if self.policy.get("campaign_ids") and cid not in self.policy["campaign_ids"]:
                continue
            try:
                c = self.campaign(cid)
                unified = c.get("UnifiedCampaign")
                if not unified:
                    result["errors"].append({"campaign_id": cid, "error": "Unsupported campaign type"})
                    continue
                strategy = unified.get("BiddingStrategy", {})
                goals = {v["GoalId"] for side in strategy.values() if isinstance(side, dict)
                         for v in side.values() if isinstance(v, dict) and "GoalId" in v}
                goals.update(g["GoalId"] for g in (unified.get("PriorityGoals") or {}).get("Items", []))
                goal_id = self.policy["goal_id"]
                attribution = unified.get("AttributionModel", "AUTO")
                c.update(goal_id=goal_id, strategy_goals=sorted(goals), attribution=attribution,
                         placement=YandexDirectClient.infer_unified_campaign_placement_type(unified))
                c["daily"] = self.report(cid, start.isoformat(), end.isoformat(), "CAMPAIGN_PERFORMANCE_REPORT", ["Date", "CampaignId"], goal_id, attribution, goals)
                c["current"] = totals([r for r in c["daily"] if r["Date"] >= split.isoformat()])
                c["previous"] = totals([r for r in c["daily"] if r["Date"] < split.isoformat()])
                c["groups"] = self.entities("adgroups", "AdGroups", {"SelectionCriteria": {"CampaignIds": [cid]}, "FieldNames": ["Id", "Name", "CampaignId", "NegativeKeywords", "Status", "ServingStatus"]})
                c["ads"] = self.entities("ads", "Ads", {"SelectionCriteria": {"CampaignIds": [cid]}, "FieldNames": ["Id", "AdGroupId", "CampaignId", "State", "Status", "Type"], "TextAdFieldNames": ["Title", "Title2", "Text", "Href", "AdImageHash"]})
                c["queries"] = self.report(cid, split.isoformat(), end.isoformat(), "SEARCH_QUERY_PERFORMANCE_REPORT", ["CampaignId", "AdGroupId", "Query"], goal_id, attribution, goals) if c["placement"] != "network_only" else []
                c["ad_performance"] = self.report(cid, split.isoformat(), end.isoformat(), "AD_PERFORMANCE_REPORT", ["AdId", "AdGroupId", "CampaignId", "AdNetworkType"], goal_id, attribution)
                c["placements"] = self.report(cid, split.isoformat(), end.isoformat(), "CUSTOM_REPORT", ["CampaignId", "AdNetworkType", "Placement"], goal_id, attribution) if c["placement"] != "search_only" else []
                c["metrika_errors"] = []
                for key, dimension in [("metrika_queries", "ym:s:lastsignDirectSearchPhrase"), ("landing_pages", "ym:s:startURLPath")]:
                    try:
                        c[key] = self.metrika_report(cid, split.isoformat(), end.isoformat(), goal_id, dimension)
                    except Exception as exc:
                        c["metrika_errors"].append(str(exc)[:600])
                result["campaigns"].append(c)
            except Exception as exc:
                result["errors"].append({"campaign_id": cid, "error": str(exc)[:900]})
        return result


def totals(rows):
    values = {key: round(sum(r[key] or 0 for r in rows), 4) for key in ("Clicks", "Cost", "Impressions", "Conversions")}
    values["UnknownConversionRows"] = sum(r["Conversions"] is None for r in rows)
    values["ConversionsComplete"] = values["UnknownConversionRows"] == 0
    goal_ids = {g for r in rows for g in r.get("conversions_by_goal", {})}
    values["GoalTotals"] = {g: {"confirmed_sum": round(sum((r.get("conversions_by_goal", {}).get(g) or 0) for r in rows), 4),
                              "unknown_rows": sum(r.get("conversions_by_goal", {}).get(g) is None for r in rows)} for g in sorted(goal_ids)}
    values["CPA"] = round(values["Cost"]/values["Conversions"], 2) if values["Conversions"] and values["ConversionsComplete"] else None
    return values


def sufficient(snapshot, previous, policy):
    if not snapshot["campaigns"]:
        return False, "no_complete_campaigns"
    rows = [r for c in snapshot["campaigns"] for r in c["daily"] if r["Date"] >= snapshot["current_from"]]
    stats = totals(rows)
    if stats["Clicks"] < policy["min_clicks"] and stats["Conversions"] < policy["min_conversions"]:
        return False, "insufficient_mature_data"
    if previous:
        old_to = previous["date_to"]
        new = totals([r for r in rows if r["Date"] > old_to])
        old_conversions = sum(c["current"]["Conversions"] for c in previous["campaigns"])
        revised = abs(stats["Conversions"] - old_conversions)
        if new["Clicks"] < policy["min_new_clicks"] and new["Conversions"] < policy["min_new_conversions"] and revised < policy["min_new_conversions"]:
            return False, "insufficient_new_data"
    return True, "ready"
