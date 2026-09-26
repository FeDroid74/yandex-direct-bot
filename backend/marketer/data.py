import csv
import hashlib
import io
import json
import math
import time
import urllib.request
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from marketer.diagnostics import campaign_diagnostics
from services.direct_client import YandexDirectClient, YandexDirectClientError
from services.metrika_client import YandexMetrikaClient, YandexMetrikaClientError


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

    def entities(self, service, key, params, allow_empty_result=False):
        rows, offset = [], 0
        for _ in range(200):
            for attempt in range(3):
                response = self.direct.call_v501(service, "get", {**params, "Page": {"Limit": 1000, "Offset": offset}})
                error = response.body.get("error") if isinstance(response.body, dict) else None
                if isinstance(error, dict) and error.get("error_code") == 1000 and attempt < 2:
                    time.sleep(2 ** attempt)
                    continue
                break
            result = checked(response.body)
            page = [] if allow_empty_result and result == {} else result.get(key)
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
                if any(None in row or any(row.get(key) is None for key in required) for row in rows):
                    raise ValueError("Truncated or malformed Direct TSV row")
            for row in rows:
                conversion = row.get(f"Conversions_{goal_id}_{attribution}")
                if conversion is None:
                    raise ValueError("Report missing selected-goal conversions")
                row["Conversions"] = number(conversion)
                if any(f"Conversions_{g}_{attribution}" not in row for g in goals):
                    raise ValueError("Report missing one of the selected strategy goals")
                row["conversions_by_goal"] = {str(g): number(row.get(f"Conversions_{g}_{attribution}")) for g in goals}
                row["raw_conversions_by_goal"] = {str(g): row[f"Conversions_{g}_{attribution}"] for g in goals}
                for name in ("Clicks", "Cost", "Impressions"):
                    row[name] = number(row.get(name))
                if any(row[k] is None for k in ("Clicks", "Cost", "Impressions")):
                    raise ValueError("Unknown report metric; not equivalent to zero")
            return rows
        raise ValueError("Report not ready")

    def metrika_report(self, cid, start, end, goal_id, dimension=None, extra_goals=None, _batch_size=7):
        goals = list(dict.fromkeys([goal_id] + list(extra_goals or [])))
        if len(goals) > _batch_size:
            combined = None
            for offset in range(0, len(goals), _batch_size):
                batch = goals[offset:offset+_batch_size]
                part = self.metrika_report(cid, start, end, batch[0], dimension, batch[1:])
                if combined is None:
                    combined = part
                    continue
                previous = {r["dimension_key"]: r for r in combined["rows"]}
                current = {r["dimension_key"]: r for r in part["rows"]}
                if set(previous) != set(current) or combined["totals"]["visits"] != part["totals"]["visits"]:
                    raise ValueError("Metrika changed between goal batches; collect again")
                for key, row in current.items():
                    if any(previous[key][metric] != row[metric] for metric in ("visits", "users", "bounce_rate", "page_depth", "duration")):
                        raise ValueError("Metrika metrics changed between goal batches")
                    previous[key]["goals"].update(row["goals"])
                combined["totals"]["goals"].update(part["totals"]["goals"])
            return combined
        metrics = ["ym:s:visits", "ym:s:users", "ym:s:bounceRate", "ym:s:pageDepth",
                   "ym:s:avgVisitDurationSeconds"]
        metrics += [f"ym:s:goal{g}{metric}" for g in goals for metric in ("reaches", "visits")]
        params = {"ids": str(self.policy["counter_id"]), "date1": start, "date2": end,
                  "metrics": ",".join(metrics), "accuracy": "full",
                  "limit": 1000, "lang": "ru", "sort": "-ym:s:visits"}
        if dimension:
            params["dimensions"] = dimension
        if cid is not None:
            params["filters"] = f"ym:s:lastsignDirectClickOrder=='{cid}'"

        def parsed(values):
            if len(values) != len(metrics) or any(number(v) is None or number(v) < 0 for v in values):
                raise ValueError("Unknown Metrika metric")
            values = [number(v) for v in values]
            return {**dict(zip(["visits", "users", "bounce_rate", "page_depth", "duration"], values[:5])),
                    "goal_reaches": values[5],
                    "goals": {str(g): {"reaches": values[5+2*i], "visits": values[6+2*i]} for i, g in enumerate(goals)}}

        rows = []
        total_metrics = None
        for _ in range(100):
            try:
                result = self.metrika._get_json("/stat/v1/data", {**params, "offset": len(rows)+1})
            except YandexMetrikaClientError as exc:
                complex_query = exc.status == 400 and any(
                    e.get("error_type") == "query_error" and str(e.get("message", "")).startswith("Запрос слишком сложный")
                    for e in exc.payload.get("errors", []) if isinstance(e, dict))
                if len(goals) > 1 and complex_query:
                    return self.metrika_report(cid, start, end, goal_id, dimension, goals[1:], _batch_size=1)
                raise
            if result.get("sampled") or result.get("total_rows_rounded"):
                raise ValueError("Metrika returned sampled/rounded data")
            page = result.get("data")
            if not isinstance(page, list) or "total_rows" not in result:
                raise ValueError("Invalid Metrika report")
            current_totals = parsed(result.get("totals", []))
            if total_metrics is not None and total_metrics != current_totals:
                raise ValueError("Metrika totals changed during pagination")
            total_metrics = current_totals
            for entry in page:
                dimensions = entry.get("dimensions", [])
                if dimension and len(dimensions) != 1:
                    raise ValueError("Missing Metrika dimension")
                key = json.dumps(dimensions, ensure_ascii=False, sort_keys=True)
                rows.append({"label": dimensions[0].get("name") if dimension else None,
                             "dimension_key": key, **parsed(entry.get("metrics", []))})
            if len(rows) >= int(result["total_rows"]):
                if len(rows) != int(result["total_rows"]) or len({r["dimension_key"] for r in rows}) != len(rows):
                    raise ValueError("Duplicate or inconsistent Metrika rows")
                return {"rows": rows, "totals": total_metrics, "attribution": "lastsign" if cid is not None else "all_traffic",
                        "dimension": dimension, "sampled": False, "date_from": start, "date_to": end}
            if not page:
                raise ValueError("Incomplete Metrika pagination")
        raise ValueError("Metrika pagination limit reached")

    def collect(self, today=None):
        today = today or datetime.now(ZoneInfo(self.policy.get("report_timezone", "Europe/Moscow"))).date()
        end = today - timedelta(days=self.policy["conversion_lag_days"]+1)
        start = end - timedelta(days=self.policy["lookback_days"]*2-1)
        split = end - timedelta(days=self.policy["lookback_days"]-1)
        campaigns = self.entities("campaigns", "Campaigns", {
            "SelectionCriteria": {"States": ["ON", "SUSPENDED"]},
            "FieldNames": ["Id", "Name", "State", "Type"],
        })
        result = {"schema_version": 2, "date_from": start.isoformat(), "date_to": end.isoformat(), "current_from": split.isoformat(),
                  "created": time.time(), "campaigns": [], "errors": [],
                  "limits": ["У бота нет данных CRM, маржи и возвратов. Ecommerce-покупка не подтверждает оплату заказа; корзина не является продажей.",
                             "Директ Conversions считает целевые визиты, Метрика отдельно возвращает целевые визиты и число достижений. Нельзя складывать разные цели: один визит может достичь нескольких.",
                             "Прочерк в Директе означает отсутствие числового значения в этом отчёте, не доказанную поломку цели. Он не подменяется нулём. Итоги берутся из отдельного отчёта за период, а не из суммы дней с прочерками.",
                             "Метрика: lastsign, Директ: атрибуция кампании. Их числа нельзя складывать.",
                             "Недавние дни исключены для дозревания конверсий; это не гарантирует полноту поздних конверсий."]}
        measurement = {"counter_id": self.policy["counter_id"], "goals": {}, "errors": []}
        result["measurement"] = measurement
        try:
            counter = self.metrika.get_counter(self.policy["counter_id"])["counter"]
            measurement["counter"] = {k: counter.get(k) for k in ("id", "site", "status", "code_status", "time_zone_name")}
            catalog = self.metrika.get_goals(self.policy["counter_id"])["goals"]
            measurement["goals"] = {str(g["id"]): {k: g[k] for k in ("id", "name", "type", "status", "conditions", "goal_source") if k in g} for g in catalog}
            measurement["catalog_complete"] = True
        except Exception as exc:
            measurement["errors"].append(str(exc)[:600])
            measurement["catalog_complete"] = False
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
                observed_goals = sorted(goals | {goal_id})
                c["daily"] = self.report(cid, start.isoformat(), end.isoformat(), "CAMPAIGN_PERFORMANCE_REPORT", ["Date", "CampaignId"], goal_id, attribution, goals)
                periods = {"current": (split.isoformat(), end.isoformat()), "previous": (start.isoformat(), (split-timedelta(days=1)).isoformat())}
                c["period_reports"] = {}
                for period, (period_start, period_end) in periods.items():
                    aggregate = self.report(cid, period_start, period_end, "CAMPAIGN_PERFORMANCE_REPORT", ["CampaignId"], goal_id, attribution, goals)
                    c["period_reports"][period] = aggregate
                    c[period] = totals(aggregate, observed_goals)
                    c[period]["source"] = "direct_period_report"
                c["groups"] = self.entities("adgroups", "AdGroups", {"SelectionCriteria": {"CampaignIds": [cid]}, "FieldNames": ["Id", "Name", "CampaignId", "NegativeKeywords", "Status", "ServingStatus"]})
                c["ads"] = self.entities("ads", "Ads", {"SelectionCriteria": {"CampaignIds": [cid]}, "FieldNames": ["Id", "AdGroupId", "CampaignId", "State", "Status", "Type"], "TextAdFieldNames": ["Title", "Title2", "Text", "Href", "AdImageHash"], "ShoppingAdFieldNames": ["FeedId", "DefaultTexts", "FeedFilterConditions"]})
                c["queries"] = self.report(cid, split.isoformat(), end.isoformat(), "SEARCH_QUERY_PERFORMANCE_REPORT", ["CampaignId", "AdGroupId", "Query"], goal_id, attribution, goals) if c["placement"] != "network_only" else []
                c["ad_performance"] = self.report(cid, split.isoformat(), end.isoformat(), "AD_PERFORMANCE_REPORT", ["AdId", "AdGroupId", "CampaignId", "AdNetworkType"], goal_id, attribution, goals)
                c["placements"] = self.report(cid, split.isoformat(), end.isoformat(), "CUSTOM_REPORT", ["CampaignId", "AdNetworkType", "Placement"], goal_id, attribution, goals) if c["placement"] != "search_only" else []
                c["metrika_errors"] = []
                c["metrika_periods"] = {}
                for period, (period_start, period_end) in periods.items():
                    try:
                        c["metrika_periods"][period] = self.metrika_report(cid, period_start, period_end, goal_id, extra_goals=goals)["totals"]
                    except Exception as exc:
                        c["metrika_errors"].append({"report": period, "error": str(exc)[:600]})
                for key, dimension in [("metrika_queries", "ym:s:lastsignDirectSearchPhrase"), ("landing_pages", "ym:s:startURLPath")]:
                    try:
                        c[key] = self.metrika_report(cid, split.isoformat(), end.isoformat(), goal_id, dimension, goals)
                    except Exception as exc:
                        c["metrika_errors"].append({"report": key, "error": str(exc)[:600]})
                result["campaigns"].append(c)
            except Exception as exc:
                result["errors"].append({"campaign_id": cid, "error": str(exc)[:900]})
        all_goals = sorted({self.policy["goal_id"]} | {g for c in result["campaigns"] for g in c["strategy_goals"]} |
                           {int(g) for g in measurement["goals"]})
        for period, period_start, period_end in (("current", split.isoformat(), end.isoformat()), ("previous", start.isoformat(), (split-timedelta(days=1)).isoformat())):
            try:
                measurement[period] = self.metrika_report(None, period_start, period_end, self.policy["goal_id"], extra_goals=all_goals)["totals"]
            except Exception as exc:
                measurement["errors"].append({"report": period, "error": str(exc)[:600]})
        for c in result["campaigns"]:
            c["diagnostics"] = campaign_diagnostics(c, measurement)
        return result


def totals(rows, goals=()):
    values = {key: round(sum(r[key] or 0 for r in rows), 4) for key in ("Clicks", "Cost", "Impressions", "Conversions")}
    values["UnknownConversionRows"] = sum(r["Conversions"] is None for r in rows)
    values["ConversionsComplete"] = bool(rows) and values["UnknownConversionRows"] == 0
    values["Rows"] = len(rows)
    goal_ids = {str(g) for g in goals} | {g for r in rows for g in r.get("conversions_by_goal", {})}
    values["GoalTotals"] = {g: {"confirmed_sum": round(sum((r.get("conversions_by_goal", {}).get(g) or 0) for r in rows), 4),
                              "unknown_rows": sum(r.get("conversions_by_goal", {}).get(g) is None for r in rows)} for g in sorted(goal_ids)}
    for goal in values["GoalTotals"].values():
        goal["complete"] = bool(rows) and goal["unknown_rows"] == 0
        goal["cpa"] = round(values["Cost"]/goal["confirmed_sum"], 2) if goal["complete"] and goal["confirmed_sum"] > 0 else None
    values["CPA"] = round(values["Cost"]/values["Conversions"], 2) if values["Conversions"] and values["ConversionsComplete"] else None
    return values


def sufficient(snapshot, previous, policy):
    if not snapshot["campaigns"]:
        return False, "no_complete_campaigns"
    rows = [r for c in snapshot["campaigns"] for r in c["daily"] if r["Date"] >= snapshot["current_from"]]
    stats = totals(rows)
    observed = max([stats["Conversions"]] + [g["confirmed_sum"] for g in stats["GoalTotals"].values()])
    if stats["Clicks"] < policy["min_clicks"] and observed < policy["min_conversions"]:
        return False, "insufficient_mature_data"
    if previous:
        if snapshot.get("schema_version", 1) > previous.get("schema_version", 1):
            return True, "analysis_data_upgraded"
        old_to = previous["date_to"]
        new = totals([r for r in rows if r["Date"] > old_to])
        old_conversions = sum(c["current"]["Conversions"] for c in previous["campaigns"])
        revised = abs(stats["Conversions"] - old_conversions)
        new_observed = max([new["Conversions"]] + [g["confirmed_sum"] for g in new["GoalTotals"].values()])
        def goal_signals(data):
            return {(c["Id"], gid): (g["confirmed_sum"], g.get("complete", g.get("unknown_rows", 0) == 0))
                    for c in data["campaigns"] for gid, g in c["current"].get("GoalTotals", {}).items()}
        changed_goals = goal_signals(snapshot) != goal_signals(previous)
        if new["Clicks"] < policy["min_new_clicks"] and new_observed < policy["min_new_conversions"] and revised < policy["min_new_conversions"] and not changed_goals:
            return False, "insufficient_new_data"
    return True, "ready"
