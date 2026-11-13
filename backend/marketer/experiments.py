"""Read-only, matched-window observations; no causal or paid-sales claims."""
import copy
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from marketer.data import totals
from marketer.errors import safe_error


def configuration(campaign):
    unified = campaign.get("UnifiedCampaign", {})
    return {k: copy.deepcopy(unified.get(k)) for k in
            ("BiddingStrategy", "PackageBiddingStrategy", "CounterIds", "AttributionModel", "PriorityGoals")}


def windows(row, policy, now):
    zone = ZoneInfo(policy.get("report_timezone", "Europe/Moscow"))
    applied = datetime.fromtimestamp(row["applied_at"], zone).date()
    mature_end = datetime.fromtimestamp(now, zone).date() - timedelta(days=policy["conversion_lag_days"]+1)
    start = applied + timedelta(days=1)
    available_days = (mature_end-start).days+1
    if available_days < row["body"]["evaluate_after_days"]:
        return None
    days = min(90, available_days)
    return {"before_from": (applied-timedelta(days=days)).isoformat(), "before_to": (applied-timedelta(days=1)).isoformat(),
            "date_from": start.isoformat(), "date_to": (start+timedelta(days=days-1)).isoformat(), "days": days}


def compare(before, after, confounders, minimum_clicks=100, minimum_conversions=10):
    if not before.get("ConversionsComplete") or not after.get("ConversionsComplete"):
        return "insufficient_data", "Итог по цели неполон. Прочерки не заменены нулями; вывод об эффективности недоступен."
    if min(before["Clicks"], after["Clicks"]) < minimum_clicks or min(before["Conversions"], after["Conversions"]) < minimum_conversions:
        return "insufficient_data", "Выборка по покупке мала. Наблюдение сохранено, успех или провал эксперимента не установлен."
    if confounders:
        return "confounded", "Есть другие изменения или непроверенные исходные настройки. Нельзя приписать результат этому действию."
    return "observed", "Есть сопоставимое наблюдение до/после, но это не A/B-тест и не доказательство причинного эффекта. Решение о продолжении принимает владелец."


class Experiments:
    def __init__(self, store, source, telegram, policy):
        self.store, self.source, self.telegram, self.policy = store, source, telegram, policy

    def other_changes(self, row, period):
        zone = ZoneInfo(self.policy.get("report_timezone", "Europe/Moscow"))
        start = datetime.fromisoformat(period["before_from"]).replace(tzinfo=zone).timestamp()
        end = (datetime.fromisoformat(period["date_to"]).replace(tzinfo=zone)+timedelta(days=1)).timestamp()
        with self.store.db() as db:
            return [{"proposal_id": r["proposal_id"], "at": r["at"], "event": r["event"]} for r in db.execute(
                "SELECT e.proposal_id,e.at,e.event FROM events e JOIN proposals p ON p.id=e.proposal_id "
                "WHERE json_extract(p.body,'$.campaign_id')=? AND e.proposal_id<>? AND e.at>=? AND e.at<? "
                "AND (e.event IN ('applied','uncertain') OR (e.event='done' AND json_extract(e.data,'$.state')='completed_manual')) ORDER BY e.at",
                (row["body"]["campaign_id"], row["id"], start, end))]

    def assess(self, row, period):
        body, evidence = row["body"], row["body"]["evidence"]
        cid, goal = body["campaign_id"], evidence["goal_id"]
        campaign = self.source.campaign(cid)
        attribution = campaign.get("UnifiedCampaign", {}).get("AttributionModel", "AUTO")
        base = {**period, "goal_id": goal, "source": "direct_period_report", "attribution": attribution,
                "paid_orders_available": False, "causal_effect_proven": False, "confounders": [],
                "limits": ["Ecommerce-покупка не подтверждает оплату; данных прибыли, возвратов и CRM нет.",
                           "Сезонность, обучение, задержки конверсий и незафиксированные внешние изменения не исключены.",
                           "Пороги выборки являются операционными, а не тестом статистической значимости."]}
        if attribution != evidence.get("attribution", attribution) or goal != self.policy["goal_id"]:
            return {**base, "status": "not_comparable", "conclusion": "Цель или атрибуция отличаются от исходных условий. Сравнение эффективности заблокировано."}, True
        expected = copy.deepcopy(evidence.get("configuration"))
        action = body["action"]
        if expected is None:
            base["confounders"].append("baseline_configuration_unavailable")
        else:
            if action["kind"] == "strategy_value":
                expected["BiddingStrategy"][action["side"]]["PayForConversion"][action["field"]] = action["value_micros"]
            if configuration(campaign) != expected:
                base["confounders"].append("campaign_configuration_changed")
        changes = self.other_changes(row, period)
        if changes:
            base["confounders"].append("other_recorded_campaign_changes")
        base["other_changes"] = changes[:20]
        base["other_changes_count"] = len(changes)
        if action["kind"] == "ad_text":
            ad = self.source.ad(action["ad_id"])
            if any(ad.get("TextAd", {}).get(k) != v for k, v in action["fields"].items()):
                base["confounders"].append("approved_ad_text_no_longer_current")
        if action["kind"] == "product_ad_edit" and set(action.get("patch", {})) == {"default_text"}:
            ad = self.source.ad(action["ad_id"])
            original = next((a for a in body.get("before", {}).get("ads", []) if a["Id"] == action["ad_id"]), {})
            shopping = ad.get("ShoppingAd", {})
            if shopping.get("DefaultTexts") != [action["patch"]["default_text"]]:
                base["confounders"].append("approved_shopping_text_no_longer_current")
            if any(shopping.get(k) != original.get("ShoppingAd", {}).get(k) for k in ("FeedId", "FeedFilterConditions")):
                base["confounders"].append("shopping_feed_or_selection_changed")
        if action["kind"] == "add_negative":
            group = self.source.group(action["ad_group_id"])
            if action["phrase"] not in (group.get("NegativeKeywords") or {}).get("Items", []):
                base["confounders"].append("approved_negative_no_longer_present")
        goals = sorted(set(evidence.get("strategy_goals", [])) | {goal})
        periods = [("after", period["date_from"], period["date_to"])]
        if action["kind"] != "product_launch":
            periods.insert(0, ("before", period["before_from"], period["before_to"]))
        for label, start, end in periods:
            base[label] = totals(self.source.report(cid, start, end, "CAMPAIGN_PERFORMANCE_REPORT", ["CampaignId"], goal, attribution, goals), goals)
        if action["kind"] == "product_launch":
            status, conclusion = "pilot_observation", "Наблюдение после запуска товарного пилота. Его нельзя считать приростом относительно старой кампании или доказанной продажей."
            if not base["after"]["ConversionsComplete"] or base["after"]["Conversions"] < self.policy.get("experiment_min_conversions", 10):
                status, conclusion = "insufficient_data", "Пилот ещё не накопил достаточную полную выборку покупки. Вывод об эффективности недоступен."
        else:
            status, conclusion = compare(base["before"], base["after"], base["confounders"],
                                         self.policy.get("experiment_min_clicks", 100), self.policy.get("experiment_min_conversions", 10))
        final = status != "insufficient_data" or period["days"] == 90
        if status == "insufficient_data" and final:
            status = "inconclusive"
            conclusion += " За 90 зрелых дней достаточных оснований не получено. Автоматические изменения не выполнялись."
        base.update(status=status, conclusion=conclusion)
        return base, final

    def run(self, now=None):
        now = time.time() if now is None else now
        with self.store.db() as db:
            rows = [self.store.unpack(r) for r in db.execute("SELECT * FROM proposals WHERE state='applied' AND evaluated_at IS NULL ORDER BY applied_at")]
        for row in rows:
            body = row["body"]
            if (row["result"] or {}).get("already_present"):
                self.store.observe(row["id"], {"status": "not_changed", "conclusion": "Изменение уже существовало; эксперимент не начат."}, final=True)
                continue
            copy_test = body["action"]["kind"] == "product_ad_edit" and set(body["action"].get("patch", {})) == {"default_text"}
            if body["action"]["kind"].startswith("product_") and body["action"]["kind"] != "product_launch" and not copy_test:
                self.store.observe(row["id"], {"status": "operational_only", "conclusion": "Выполнена настройка, а не эксперимент с продажами."}, final=True)
                continue
            period = windows(row, self.policy, now)
            if not period:
                continue
            previous = self.store.observation(row["id"])
            if previous and now-previous["updated"] < 7*86400:
                continue
            try:
                data, final = self.assess(row, period)
            except Exception as exc:
                data, final = {**period, "status": "data_unavailable", "conclusion": "Данные для оценки недоступны. Это не нулевой результат эксперимента.", "error": safe_error(exc)}, False
            self.store.observe(row["id"], data, final)
        self.notify()

    def notify(self):
        with self.store.db() as db:
            pending = [r[0] for r in db.execute("SELECT proposal_id FROM experiment_observations WHERE notification='pending'")]
        for pid in pending:
            row = self.store.get(pid)
            observation = self.store.observation(pid)["data"]
            if observation["status"] in ("operational_only", "not_changed"):
                self.store.observation_notice_sent(pid)
                continue
            if not self.store.claim_observation_notice(pid):
                continue
            message = f"Оценка #{pid}: {row['body']['title']}\n{observation['conclusion']}"
            if observation.get("before"):
                before = observation["before"]
                conversions = f"{before['Conversions']:g}" if before["ConversionsComplete"] else "неизвестно"
                message += f"\nДо ({observation['before_from']} — {observation['before_to']}): {before['Clicks']:g} кликов; {before['Cost']:g} руб.; целевых визитов покупки: {conversions}."
            if observation.get("after"):
                after = observation["after"]
                conversions = f"{after['Conversions']:g}" if after["ConversionsComplete"] else "неизвестно"
                message += f"\nПосле ({observation['date_from']} — {observation['date_to']}): {after['Clicks']:g} кликов; {after['Cost']:g} руб.; целевых визитов покупки: {conversions}."
                message += "\nЭто не подтверждённые оплаты и не доказанный эффект изменения."
            try:
                self.telegram.notify(message)
            except Exception:
                continue  # Delivery is uncertain; never send the same notice blindly again.
            self.store.observation_notice_sent(pid)
