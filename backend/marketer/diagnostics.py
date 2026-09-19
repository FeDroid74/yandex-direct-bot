def campaign_diagnostics(campaign, measurement):
    primary = str(campaign["goal_id"])
    goals = sorted({primary} | {str(g) for g in campaign.get("strategy_goals", [])})
    linked = measurement["counter_id"] in (campaign.get("UnifiedCampaign", {}).get("CounterIds") or {}).get("Items", [])
    result = {"counter_linked": linked, "goals": {}, "tracking_failure_proven": False,
              "independent_analysis": ["search_relevance", "ad_and_landing_alignment", "channel_structure"],
              "limits": ["Нулевое число покупок и прочерки не доказывают неисправность цели.",
                         "Проверка через API не заменяет тест реального заказа и не подтверждает правильность всех событий."]}
    for gid in goals:
        metadata = measurement.get("goals", {}).get(gid)
        present = bool(metadata) if measurement.get("catalog_complete") else None
        direct = {p: campaign.get(p, {}).get("GoalTotals", {}).get(gid) for p in ("current", "previous")}
        metrika = {p: campaign.get("metrika_periods", {}).get(p, {}).get("goals", {}).get(gid) for p in ("current", "previous")}
        counter = {p: measurement.get(p, {}).get("goals", {}).get(gid) for p in ("current", "previous")}
        observed = (counter["current"] or {}).get("reaches")
        state = "unavailable" if observed is None else ("observed_in_period" if observed > 0 else "not_observed_in_period")
        if present is False:
            state = "goal_not_found_in_counter"
        result["goals"][gid] = {"name": (metadata or {}).get("name", gid), "type": (metadata or {}).get("type"),
                                "role": "primary_business_signal" if gid == primary else "supporting_signal",
                                "status": (metadata or {}).get("status"), "present": present,
                                "counter_observation": state, "direct": direct, "metrika": metrika,
                                "counter_all_traffic": counter}
    return result
