"""Calendar purchase objective. Read-only observations never authorize ad writes."""
import calendar
import math
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from marketer.billing import COUNTER, PURCHASE_GOAL
from marketer.errors import safe_error
from marketer.store import digest


OBJECTIVE = {
    "id": "owner-2026-11-13-monthly-purchases-v1",
    "primary": "Рост числа Ecommerce-покупок из рекламных кампаний каждый календарный месяц",
    "goal_id": PURCHASE_GOAL, "counter_id": COUNTER,
    "metric": "metrika.goal_reaches", "attribution": "lastsign",
    "constraint": "Только оплата за покупку; существующие бюджеты, CPA, лимиты, согласования и cooldown не расширяются",
    "guaranteed": False, "paid_orders_confirmed": False,
}


def month_start(day):
    return day.replace(day=1)


def windows(today, lag):
    mature = today - timedelta(days=lag + 1)
    end = month_start(mature + timedelta(days=1)) - timedelta(days=1)
    months = []
    for _ in range(3):
        start = month_start(end)
        months.insert(0, {"from": start.isoformat(), "to": end.isoformat(), "month": start.strftime("%Y-%m")})
        end = start - timedelta(days=1)
    progress = []
    if month_start(mature) == month_start(today):
        current = month_start(today)
        prior = month_start(current - timedelta(days=1))
        days = min(mature.day, calendar.monthrange(prior.year, prior.month)[1])
        # Equal elapsed days, never compare an incomplete month against a full one.
        progress = [{"from": d.isoformat(), "to": (d + timedelta(days=days-1)).isoformat(), "days": days}
                    for d in (prior, current)]
    return mature.isoformat(), months, progress


def compare(before, after):
    if before is None or after is None:
        return {"status": "data_unavailable", "before": before, "after": after, "delta": None, "percent": None}
    delta = after - before
    return {"status": "increased" if delta > 0 else "decreased" if delta < 0 else "flat",
            "before": before, "after": after, "delta": delta,
            "percent": round(delta / before * 100, 2) if before > 0 else None}


def purchase_count(report):
    if report.get("sampled") is not False:
        raise ValueError("Purchase report is not confirmed unsampled")
    value = report.get("totals", {}).get("goals", {}).get(str(PURCHASE_GOAL), {}).get("reaches")
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
        raise ValueError("Purchase total is unknown, not zero")
    return value


class Growth:
    def __init__(self, source, policy):
        self.source, self.policy = source, policy

    def collect(self, snapshot, today=None):
        tz = self.policy.get("report_timezone", "Europe/Moscow")
        today = today or datetime.now(ZoneInfo(tz)).date()
        mature, months, progress = windows(today, self.policy["conversion_lag_days"])
        result = {"objective": OBJECTIVE, "created": time.time(), "as_of": today.isoformat(), "timezone": tz,
                  "mature_through": mature, "months": months, "progress_periods": progress, "errors": [],
                  "status": "data_unavailable", "diagnosis_required": True}
        try:
            measurement = snapshot.get("measurement") or {}
            goal = measurement.get("goals", {}).get(str(PURCHASE_GOAL), {})
            if (self.policy["goal_id"] != PURCHASE_GOAL or self.policy["counter_id"] != COUNTER
                or measurement.get("catalog_complete") is not True or goal.get("type") != "e_purchase"
                or goal.get("status") != "Active" or measurement.get("counter", {}).get("time_zone_name") != tz):
                raise ValueError("Ecommerce purchase catalog or report timezone not confirmed")
            # Include OFF/ARCHIVED campaigns: current survivors must not redefine prior months.
            inventory = self.source.entities("campaigns", "Campaigns", {"SelectionCriteria": {}, "FieldNames": ["Id"]})
            ids = sorted({c["Id"] for c in inventory})
            if self.policy.get("campaign_ids"):
                requested = set(self.policy["campaign_ids"])
                if not requested.issubset(ids):
                    raise ValueError("Monthly campaign scope is incomplete")
                ids = sorted(requested)
            if not ids or len(ids) > 500 or any(type(i) is not int or i <= 0 for i in ids):
                raise ValueError("Monthly campaign inventory is incomplete or exceeds query safety limit")
            result["campaign_ids"] = ids
            result["scope"] = "all_account_campaigns_including_off_and_archived" if not self.policy.get("campaign_ids") else "configured_campaign_ids"
        except Exception as exc:
            result["errors"].append(safe_error(exc, 250))
            result["comparison"] = compare(None, None)
            return result
        for period in months + progress:
            for scope in ("direct", "site"):
                try:
                    report = self.source.purchase_report(period["from"], period["to"], PURCHASE_GOAL,
                                                        campaign_ids=ids if scope == "direct" else None)
                    period[scope] = purchase_count(report)
                    period[scope + "_partitions"] = report.get("partitions", [])
                except Exception as exc:
                    period[scope] = None
                    result["errors"].append({"scope": scope, "from": period["from"], "to": period["to"], "error": safe_error(exc, 250)})
        result["comparison"] = compare(months[-2].get("direct"), months[-1].get("direct"))
        result["site_comparison"] = compare(months[-2].get("site"), months[-1].get("site"))
        result["progress"] = compare(*[p.get("direct") for p in progress]) if progress else {"status": "not_mature"}
        result["status"] = "ready" if result["comparison"]["status"] != "data_unavailable" else "data_unavailable"
        result["diagnosis_required"] = result["comparison"]["status"] != "increased"
        baseline = result["comparison"]["after"]
        result["next_month_target"] = {"month": (datetime.fromisoformat(months[-1]["to"]).date()+timedelta(days=1)).strftime("%Y-%m"),
                                       "at_least": math.floor(baseline)+1 if baseline is not None else None,
                                       "guaranteed": False}
        result["stagnant_months"] = 0
        for before, after in reversed(list(zip(months, months[1:]))):
            if compare(before.get("direct"), after.get("direct"))["status"] not in ("flat", "decreased"):
                break
            result["stagnant_months"] += 1
        result["fingerprint"] = digest({"scope": ids, "months": [{k: m.get(k) for k in ("from", "to", "direct", "site")} for m in months],
                                        "objective": OBJECTIVE["id"]})
        return result


def notice(report):
    if not report:
        return "Месячный ориентир: рост покупок из рекламы. Календарное сравнение ещё не собрано."
    pair = report["comparison"]
    if report["status"] != "ready":
        return "Месячный ориентир: рост покупок из рекламы. Данные для сравнения не подтверждены; нужен разбор недостающих данных, не вывод о нулевых покупках."
    before, after = report["months"][-2:]
    label = {"increased": "рост наблюдается", "flat": "роста нет", "decreased": "снижение"}[pair["status"]]
    text = (f"Срез на {report['as_of']}. Покупки из рекламы, Метрика (lastsign), цель {PURCHASE_GOAL}: "
            f"{before['month']} — {pair['before']:g}; {after['month']} — {pair['after']:g}: {label}. "
            f"Это достижения Ecommerce-цели, не подтверждённые оплаты. Учтена задержка; поздние конверсии возможны.")
    if pair["before"] == 0 and pair["after"] > 0:
        text += " Процентный рост от нуля не вычисляется."
    target = report.get("next_month_target") or {}
    if target.get("at_least") is not None:
        text += f" Ориентир на {target['month']}: не менее {target['at_least']} достижений покупки из рекламы, без гарантии результата."
    if report.get("progress_periods") and report["progress"]["status"] != "data_unavailable":
        p = report["progress"]
        text += (f" Текущий месяц, одинаковые первые {report['progress_periods'][0]['days']} зрелых дней: "
                 f"{p['before']:g} → {p['after']:g}; это промежуточный сигнал, не итог месяца.")
    if report["diagnosis_required"]:
        text += " Приоритет: разобрать причины и выбрать проверяемый следующий шаг с учётом прошлых экспериментов."
    else:
        text += " Продолжить контролируемые эксперименты; наблюдаемый рост не доказывает эффект последних правок."
    return text


def assessment_text(assessment, knowledge):
    if not assessment:
        return ""
    text = "Разбор: " + assessment["diagnosis"] + "\nСледующий шаг: " + assessment["next_step"]
    available = {e["id"]: e for e in knowledge.get("excerpts", [])}
    sources = [available[k] for k in assessment.get("knowledge_refs", []) if k in available]
    if sources:
        text += "\nСправочные материалы курса: " + "; ".join(e["lesson"] + " · " + e["locator"] for e in sources)
    return text
