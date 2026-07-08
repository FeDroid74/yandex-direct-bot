from typing import Dict, List, Tuple


ALLOWED_SITE = "https://artfarfor.com"
ALLOWED_LANGUAGE = "RU"
ALLOWED_GOALS = {"leads", "sales"}

# Unified campaign delivery channels supported by backend create/update flow.
ALLOWED_PLACEMENTS = {"both", "search_only", "network_only"}

# Новое жёсткое правило:
# только стратегия с оплатой за конверсии
ALLOWED_STRATEGIES = {"pay_for_conversion"}


def validate_campaign(data: Dict) -> Tuple[bool, List[str]]:
    errors: List[str] = []

    required_fields = [
        "campaign_name",
        "site_url",
        "region",
        "language",
        "placement_type",
        "goal_type",
        "strategy_type",
        "metrica_goal_id",
        "weekly_budget_rub",
        "target_cpa_rub",
    ]

    for field in required_fields:
        if field not in data or data[field] in ("", None):
            errors.append(f"missing field: {field}")

    campaign_name = data.get("campaign_name")
    if campaign_name is not None and not isinstance(campaign_name, str):
        errors.append("campaign_name must be a string")

    site_url = data.get("site_url")
    if site_url and site_url != ALLOWED_SITE:
        errors.append(f"site_url must be exactly {ALLOWED_SITE}")

    region = data.get("region")
    if region is not None and not isinstance(region, str):
        errors.append("region must be a string")

    language = data.get("language")
    if language and language != ALLOWED_LANGUAGE:
        errors.append("language must be 'RU'")

    placement_type = data.get("placement_type")
    if placement_type and placement_type not in ALLOWED_PLACEMENTS:
        errors.append("placement_type must be 'both', 'search_only', or 'network_only'")

    goal_type = data.get("goal_type")
    if goal_type and goal_type not in ALLOWED_GOALS:
        errors.append("goal_type must be 'leads' or 'sales'")

    strategy_type = data.get("strategy_type")
    if strategy_type and strategy_type not in ALLOWED_STRATEGIES:
        errors.append("strategy_type must be 'pay_for_conversion'")

    # Жёстко запрещаем legacy daily budget
    if "daily_budget" in data and data.get("daily_budget") not in ("", None):
        errors.append("daily_budget is forbidden; use weekly_budget_rub")

    weekly_budget_rub = data.get("weekly_budget_rub")
    if weekly_budget_rub is not None:
        if not isinstance(weekly_budget_rub, (int, float)):
            errors.append("weekly_budget_rub must be a number")
        elif weekly_budget_rub <= 0:
            errors.append("weekly_budget_rub must be > 0")
        elif weekly_budget_rub > 300000:
            errors.append("weekly_budget_rub must be <= 300000 for auto-flow")

    target_cpa_rub = data.get("target_cpa_rub")
    if target_cpa_rub is not None:
        if not isinstance(target_cpa_rub, (int, float)):
            errors.append("target_cpa_rub must be a number")
        elif target_cpa_rub <= 0:
            errors.append("target_cpa_rub must be > 0")
        elif target_cpa_rub > 30000:
            errors.append("target_cpa_rub must be <= 30000 for auto-flow")

    # Для pay for conversion в API недельный бюджет должен быть не меньше, чем CPA * 20
    # Это правило соответствует StrategyPayForConversionAdd.WeeklySpendLimit.
    if (
        isinstance(weekly_budget_rub, (int, float))
        and isinstance(target_cpa_rub, (int, float))
        and weekly_budget_rub > 0
        and target_cpa_rub > 0
    ):
        min_weekly_budget = target_cpa_rub * 20
        if weekly_budget_rub < min_weekly_budget:
            errors.append(
                f"weekly_budget_rub must be >= target_cpa_rub * 20 ({min_weekly_budget})"
            )

    metrica_goal_id = data.get("metrica_goal_id")
    if metrica_goal_id is not None:
        if not isinstance(metrica_goal_id, (int, str)):
            errors.append("metrica_goal_id must be string or number")
        elif str(metrica_goal_id).strip() == "":
            errors.append("metrica_goal_id must not be empty")

    schedule = data.get("schedule")
    if schedule is not None and not isinstance(schedule, str):
        errors.append("schedule must be a string")

    utm_tracking = data.get("utm_tracking")
    if utm_tracking is not None and not isinstance(utm_tracking, bool):
        errors.append("utm_tracking must be boolean")

    ad_groups = data.get("ad_groups")
    if ad_groups is not None and not isinstance(ad_groups, list):
        errors.append("ad_groups must be a list")

    ads = data.get("ads")
    if ads is not None and not isinstance(ads, list):
        errors.append("ads must be a list")

    return len(errors) == 0, errors


def create_campaign(data: Dict) -> Dict:
    """
    Legacy mock create retained only as fallback helper.
    """
    is_valid, errors = validate_campaign(data)
    if not is_valid:
        return {
            "status": "error",
            "errors": errors,
        }

    return {
        "status": "success",
        "campaign_id": "mock_12345",
        "data": data,
    }
