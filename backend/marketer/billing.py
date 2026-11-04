"""Fail closed on billing types and goals not authorized by the owner."""
from marketer.store import Conflict

PURCHASE_GOAL = 352606262
COUNTER = 99041859


def require_purchase_billing(campaign):
    unified = campaign.get("UnifiedCampaign") or {}
    if unified.get("PackageBiddingStrategy"):
        raise Conflict("Пакетная стратегия: автоматическое изменение запрещено.")
    if COUNTER not in (unified.get("CounterIds") or {}).get("Items", []):
        raise Conflict("Не подтверждена привязка счётчика покупки.")
    strategy = unified.get("BiddingStrategy") or {}
    active = 0
    for name in ("Search", "Network"):
        side = strategy.get(name) or {}
        kind = side.get("BiddingStrategyType")
        if kind == "SERVING_OFF":
            continue
        if name == "Network" and kind == "NETWORK_DEFAULT":
            side = strategy.get("Search") or {}
            kind = side.get("BiddingStrategyType")
        values = side.get("PayForConversion") or {}
        if kind != "PAY_FOR_CONVERSION" or values.get("GoalId") != PURCHASE_GOAL:
            raise Conflict("Разрешена только PAY_FOR_CONVERSION по покупке 352606262. Настройки не изменены.")
        active += 1
    if not active:
        raise Conflict("Нет подтверждённой активной стратегии оплаты за покупку.")
    return unified
