"""Read-only account review. Candidate keys never contain a JS-rounded Direct ad ID."""
from datetime import datetime
import time
from zoneinfo import ZoneInfo

from marketer.billing import PURCHASE_GOAL, require_purchase_billing
from marketer.store import Conflict, digest


def review(snapshot, now=None):
    if not snapshot:
        return {"status": "missing", "text": "Нет сохранённой статистики. Нужен новый сбор.", "opportunities": []}
    now = time.time() if now is None else now
    today = datetime.fromtimestamp(now, ZoneInfo("Europe/Moscow")).date().isoformat()
    rows, future, products, candidates, landings, drift = [], [], [], [], [], []
    fresh = 0 <= now - snapshot["created"] <= 86400
    assortment = snapshot.get("assortment") or {}
    selections = {(x["campaign_id"], x["ad_id"]): x for x in assortment.get("campaign_selections", [])}
    for c in snapshot["campaigns"]:
        cid = c["Id"]
        metrics = c.get("current", {})
        clicks = metrics.get("Clicks") or 0
        try:
            require_purchase_billing(c)
            compliant = True
        except Conflict:
            compliant = False
        if c.get("State") == "ON" and (c.get("StartDate") or "") > today and not compliant:
            future.append({"campaign_id": cid, "name": c["Name"], "start": c["StartDate"],
                           "goals": c.get("strategy_goals", [])})
        ads = c.get("ads") or []
        if not clicks or not ads:
            continue
        shopping = [a for a in ads if a.get("ShoppingAd")]
        if shopping:
            products.append({"id": cid, "clicks": clicks, "only_shopping": len(shopping) == len(ads),
                             "direct": metrics.get("GoalTotals", {}).get(str(PURCHASE_GOAL), {}),
                             "metrika": (c.get("metrika_periods",{}).get("current") or {}).get("goals",{}).get(str(PURCHASE_GOAL),{}),
                             "metrika_errors": bool(c.get("metrika_errors"))})
        network = sum(r.get("Clicks", 0) for r in c.get("ad_performance", []) if r.get("AdNetworkType") == "AD_NETWORK")
        planned_search = any(selections.get((cid,a["Id"]),{}).get("planned_placement") == "search_only" for a in shopping)
        misaligned = planned_search and c.get("UnifiedCampaign",{}).get("BiddingStrategy",{}).get("Network",{}).get("BiddingStrategyType") == "NETWORK_DEFAULT"
        if misaligned:
            drift.append((cid, c["Name"], network, clicks))
            if fresh:
                task = "Выбрать целевое размещение этой кампании: сохранить Поиск + сети либо подготовить возврат на только Поиск. Это решение о плане, а не переключение показов."
                action = {"kind":"advisory","task":task}
                candidates.append({"key": digest({"campaign":cid,"action":action})[:20], "campaign_id":cid,
                                   "name":c["Name"], "action":action, "blocked_text":True, "before":None, "after":None,
                                   "reason":f"Для {c['Name']} план был «только Поиск», сейчас NETWORK_DEFAULT. Клики из сетей: {network:g} из {clicks:g}. Исполняемую правку по старому плану готовить нельзя."})
        if c.get("State") != "ON" or (c.get("StartDate") or "") > today or not compliant:
            continue
        for a in shopping:
            ad_id = a["Id"]
            sel = selections.get((cid, ad_id)) or {}
            comp = sel.get("composition") or {}
            figures = comp.get("figurine_named", {}).get("count", 0)
            ware = comp.get("tableware_named", {}).get("count", 0)
            texts = a["ShoppingAd"].get("DefaultTexts") or []
            if (not fresh or a.get("Status") != "ACCEPTED" or a.get("State") != "ON"
                or assortment.get("status") != "ready" or sel.get("status") != "observed"
                or not figures or not ware or sel.get("available") != figures + ware
                or len(texts) != 1 or "статуэт" not in texts[0].casefold()
                or "фарфор" not in texts[0].casefold() or any(w in texts[0].casefold() for w in ("посуд", "тарел", "чайны"))):
                continue
            # Keep thematic wording only when the original ad supports it.
            new = ("Фарфор на военно-историческую тему: статуэтки и посуда. Каталог ArtFarfor."
                   if "военные статуэтки из фарфора" in texts[0].casefold()
                   else "Статуэтки и предметы посуды для коллекции. Смотрите каталог ArtFarfor.")
            reason = (f"Клики кампании за {snapshot['current_from']} — {snapshot['date_to']}: {clicks:g}. "
                      f"Сейчас фильтр содержит {figures} статуэток и {ware} предметов посуды по названиям в YML. "
                      "Предлагаю точнее описать выбор, не исключая товары. Это резервный текст: он не обязательно показывается при каждом показе.")
            action = {"kind": "product_ad_edit", "campaign_id": cid, "ad_id": ad_id, "patch": {"default_text": new}}
            edit = {"key": digest(action)[:20], "campaign_id": cid, "name": c["Name"],
                    "before": texts[0], "after": new, "reason": reason, "action": action}
            if misaligned:
                current = next(x for x in candidates if x["campaign_id"] == cid)
                current.update(before=texts[0],after=new,copy_reason=reason)
            else:
                candidates.append(edit)
        for row in (c.get("landing_pages") or {}).get("rows", []):
            visits = row.get("visits") or 0
            path = row.get("label") or ""
            if (visits >= 30 and path.startswith("/product/") and not c.get("metrika_errors")
                and row.get("goals", {}).get(str(PURCHASE_GOAL), {}).get("reaches") == 0):
                landings.append((visits, cid, path))
    if not fresh:
        rows.append("Снимок старше суток или имеет некорректное время. Перед подготовкой карточки нужен новый сбор.")
    rows.append(f"Директ, {snapshot['current_from']} — {snapshot['date_to']}: кампаний в снимке {len(snapshot['campaigns'])}; ошибки сбора: {len(snapshot.get('errors', []))}.")
    if snapshot.get("errors"):
        rows.append("Не включены в разбор: "+", ".join(str(e.get("campaign_id","неизвестная кампания")) for e in snapshot["errors"][:8])+". Нет отчёта не означает ноль показов.")
    product_only = [c for c in products if c["only_shopping"]]
    if product_only:
        rows.append(f"Кампаний с кликами и только товарными объявлениями: {len(product_only)}. Автоправка текста обычного объявления к ним не подходит. Текст товарного объявления можно изменить по отдельной карточке.")
        incomplete = sum(p["direct"].get("complete") is not True for p in product_only)
        if incomplete:
            rows.append(f"Директ: неполный итог по покупке в {incomplete} из {len(product_only)} товарных кампаний с кликами. Для них CPA по покупке не оценивается.")
        if all(not p["metrika_errors"] and p["metrika"].get("reaches") == 0 for p in product_only):
            rows.append("Метрика (lastsign): по этим товарным кампаниям не зарегистрировано достижений цели покупки. Это не вывод об оплаченных заказах.")
    if candidates:
        pick = next((x for x in candidates if x.get("after")),candidates[0])
        rows.append(pick["reason"])
        if pick.get("after"):
            if pick.get("copy_reason"):
                rows.append(pick["copy_reason"])
            rows.append(("После сверки размещения можно предложить резервный текст: «" if pick.get("blocked_text") else "Предлагаемый резервный текст: «")+pick["after"]+"».")
    if drift:
        rows.append("План «только Поиск» расходится с настройкой сетей. Клики из сетей / всего по кампаниям: "
                    + "; ".join(f"{name}: {n:g}/{total:g}" for _,name,n,total in drift[:5])+". Это не основание автоматически отключать сети.")
    if future:
        first = min(item["start"] for item in future)
        rows.append(f"До запуска: {len(future)} кампаний с будущей датой старта и без подтверждения оплаты только за покупку; ближайший старт {first}. Для изменения стратегии потребуется отдельное решение по каждой кампании.")
    if landings:
        visits, cid, path = max(landings)
        rows.append(f"Гипотеза по посадочной: {cid}, {path[:160]}: {visits:g} визитов, 0 достижений цели покупки в Метрике (lastsign). Проверить мобильный путь от карточки до корзины. Это не доказательство неисправности и не число оплаченных заказов.")
    rows.append("Это разбор возможностей, не применение изменений. Отсутствие автоматической правки не означает, что кампании оптимальны.")
    return {"status": "ready" if fresh else "stale", "run_id": snapshot["run_id"], "text": "\n\n".join(rows),
            "future_billing_review": future, "opportunities": candidates}


def public_review(result):
    return {**result, "opportunities": [{k: v for k, v in item.items() if k != "action"} for item in result["opportunities"]]}
