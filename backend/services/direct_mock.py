from typing import Dict, List, Tuple


ALLOWED_SITE = "https://artfarfor.com"
ALLOWED_LANGUAGE = "RU"
ALLOWED_GOALS = {"leads", "sales"}
ALLOWED_PLACEMENTS = {"search", "network", "both"}
ALLOWED_STRATEGIES = {
    "weekly_clicks_conversion_maximization",
    "weekly_conversions_maximization",
    "pay_per_conversion",
    "target_cpa",
    "target_drr",
}


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
        "daily_budget",
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
        errors.append("placement_type must be 'search', 'network', or 'both'")

    goal_type = data.get("goal_type")
    if goal_type and goal_type not in ALLOWED_GOALS:
        errors.append("goal_type must be 'leads' or 'sales'")

    strategy_type = data.get("strategy_type")
    if strategy_type and strategy_type not in ALLOWED_STRATEGIES:
        errors.append("strategy_type must be conversion-based and allowed")

    daily_budget = data.get("daily_budget")
    if daily_budget is not None:
        if not isinstance(daily_budget, (int, float)):
            errors.append("daily_budget must be a number")
        elif daily_budget <= 0:
            errors.append("daily_budget must be > 0")
        elif daily_budget > 30000:
            errors.append("daily_budget must be <= 30000 for auto-flow")

    metrica_goal_id = data.get("metrica_goal_id")
    if metrica_goal_id is not None and not isinstance(metrica_goal_id, (int, str)):
        errors.append("metrica_goal_id must be string or number")

    ad_groups = data.get("ad_groups")
    if ad_groups is not None and not isinstance(ad_groups, list):
        errors.append("ad_groups must be a list")

    ads = data.get("ads")
    if ads is not None and not isinstance(ads, list):
        errors.append("ads must be a list")

    return len(errors) == 0, errors


def create_campaign(data: Dict) -> Dict:
    print("=== CREATE CAMPAIGN ===")

    is_valid, errors = validate_campaign(data)
    if not is_valid:
        return {
            "status": "error",
            "errors": errors,
        }

    campaign_id = "mock_12345"

    return {
        "status": "success",
        "campaign_id": campaign_id,
        "data": data,
    }