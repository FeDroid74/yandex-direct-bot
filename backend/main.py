from datetime import date, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
from typing import Optional

from config import settings
from services.direct_client import YandexDirectClient, YandexDirectClientError
from services.direct_mock import validate_campaign


ALLOWED_TARGETS = {"sandbox", "production"}
ALLOWED_OFFER_RETARGETING = {"YES", "NO"}
DEFAULT_METRICA_COUNTER_ID = 99041859


def rub_to_micros(value_rub: float) -> int:
    return int(float(value_rub) * 1_000_000)


def micros_to_rub(value_micros: int) -> float:
    return float(value_micros) / 1_000_000


def resolve_start_date(payload: dict) -> str:
    raw = payload.get("start_date")
    if isinstance(raw, str) and raw.strip():
        return raw.strip()
    return (date.today() + timedelta(days=1)).isoformat()


def resolve_target(payload: dict) -> str:
    raw = payload.get("target", "sandbox")
    if not isinstance(raw, str):
        return ""
    return raw.strip().lower()


def resolve_confirm(payload: dict) -> bool:
    value = payload.get("confirm", False)

    if isinstance(value, bool):
        return value

    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y"}

    if isinstance(value, (int, float)):
        return bool(value)

    return False


def normalize_campaign_id(value):
    if isinstance(value, bool):
        return None

    if isinstance(value, int):
        return value if value > 0 else None

    if isinstance(value, str):
        stripped = value.strip()
        if not stripped.isdigit():
            return None
        parsed = int(stripped)
        return parsed if parsed > 0 else None

    return None


def normalize_ad_group_id(value):
    if isinstance(value, bool):
        return None

    if isinstance(value, int):
        return value if value > 0 else None

    if isinstance(value, str):
        stripped = value.strip()
        if not stripped.isdigit():
            return None
        parsed = int(stripped)
        return parsed if parsed > 0 else None

    return None


def normalize_ad_id(value):
    if isinstance(value, bool):
        return None

    if isinstance(value, int):
        return value if value > 0 else None

    if isinstance(value, str):
        stripped = value.strip()
        if not stripped.isdigit():
            return None
        parsed = int(stripped)
        return parsed if parsed > 0 else None

    return None


def normalize_positive_number(value) -> Optional[float]:
    if isinstance(value, bool):
        return None

    if isinstance(value, (int, float)):
        numeric = float(value)
        return numeric if numeric > 0 else None

    if isinstance(value, str):
        stripped = value.strip().replace(",", ".")
        if not stripped:
            return None
        try:
            numeric = float(stripped)
        except ValueError:
            return None
        return numeric if numeric > 0 else None

    return None


def normalize_region_ids(value):
    if not isinstance(value, list) or not value:
        return None

    normalized = []
    for item in value:
        if isinstance(item, bool):
            return None
        if isinstance(item, int):
            normalized.append(item)
            continue
        if isinstance(item, str):
            stripped = item.strip()
            if stripped.startswith("-"):
                number_part = stripped[1:]
                if not number_part.isdigit():
                    return None
                normalized.append(-int(number_part))
                continue
            if not stripped.isdigit():
                return None
            normalized.append(int(stripped))
            continue
        return None

    return normalized


def normalize_negative_keywords(value):
    if value is None:
        return None

    if not isinstance(value, list):
        return None

    result = []
    for item in value:
        if not isinstance(item, str):
            return None
        stripped = item.strip()
        if not stripped:
            return None
        result.append(stripped)

    return result


def resolve_offer_retargeting(payload: dict) -> str:
    raw = payload.get("offer_retargeting", "NO")
    if not isinstance(raw, str):
        return ""
    return raw.strip().upper()


def parse_add_result(result: dict, entity_name: str) -> dict:
    add_results = result.get("result", {}).get("AddResults", [])
    if not add_results:
        return {
            "ok": False,
            "status": 502,
            "payload": {
                "status": "error",
                "message": f"empty AddResults from Yandex Direct for {entity_name}",
                "raw": result,
            },
        }

    first = add_results[0]

    if "Errors" in first:
        return {
            "ok": False,
            "status": 400,
            "payload": {
                "status": "error",
                "message": f"Yandex Direct rejected {entity_name} create",
                "errors": first["Errors"],
                "raw": result,
            },
        }

    return {
        "ok": True,
        "status": 200,
        "payload": {
            "status": "success",
            "id": str(first["Id"]),
        },
    }


def parse_update_result(result: dict, entity_name: str) -> dict:
    update_results = result.get("result", {}).get("UpdateResults", [])
    if not update_results:
        return {
            "ok": False,
            "status": 502,
            "payload": {
                "status": "error",
                "message": f"empty UpdateResults from Yandex Direct for {entity_name}",
                "raw": result,
            },
        }

    first = update_results[0]

    if "Errors" in first:
        return {
            "ok": False,
            "status": 400,
            "payload": {
                "status": "error",
                "message": f"Yandex Direct rejected {entity_name} update",
                "errors": first["Errors"],
                "raw": result,
            },
        }

    return {
        "ok": True,
        "status": 200,
        "payload": {
            "status": "success",
            "id": str(first["Id"]),
            "warnings": first.get("Warnings", []),
        },
    }


def extract_current_upc_strategy(campaign: dict) -> dict:
    bidding_strategy = (
        campaign.get("UnifiedCampaign", {})
        .get("BiddingStrategy", {})
    )

    pay_for_conversion = (
        bidding_strategy.get("Search", {})
        .get("PayForConversion", {})
    )

    goal_id = pay_for_conversion.get("GoalId")
    cpa_micros = pay_for_conversion.get("Cpa")
    weekly_budget_micros = pay_for_conversion.get("WeeklySpendLimit")

    if not isinstance(goal_id, int):
        raise YandexDirectClientError("current campaign GoalId is missing or invalid")

    if not isinstance(cpa_micros, int):
        raise YandexDirectClientError("current campaign Cpa is missing or invalid")

    if not isinstance(weekly_budget_micros, int):
        raise YandexDirectClientError("current campaign WeeklySpendLimit is missing or invalid")

    return {
        "goal_id": goal_id,
        "cpa_micros": cpa_micros,
        "weekly_budget_micros": weekly_budget_micros,
    }


class Handler(BaseHTTPRequestHandler):
    def _send_json(self, payload: dict, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        content_length = int(self.headers.get("Content-Length", "0"))
        raw_body = self.rfile.read(content_length)

        try:
            data = json.loads(raw_body.decode("utf-8"))
        except json.JSONDecodeError:
            self._send_json({"status": "error", "message": "invalid json"}, 400)
            return

        if self.path == "/validate_campaign":
            is_valid, errors = validate_campaign(data)

            if is_valid:
                self._send_json(
                    {
                        "status": "success",
                        "valid": True,
                        "errors": [],
                    },
                    200,
                )
            else:
                self._send_json(
                    {
                        "status": "error",
                        "valid": False,
                        "errors": errors,
                    },
                    400,
                )
            return

        if self.path == "/create_campaign":
            is_valid, errors = validate_campaign(data)
            if not is_valid:
                self._send_json(
                    {
                        "status": "error",
                        "valid": False,
                        "errors": errors,
                    },
                    400,
                )
                return

            target = resolve_target(data)
            if target not in ALLOWED_TARGETS:
                self._send_json(
                    {
                        "status": "error",
                        "message": "target must be 'sandbox' or 'production'",
                    },
                    400,
                )
                return

            confirm = resolve_confirm(data)
            start_date = resolve_start_date(data)

            if target == "production" and not confirm:
                self._send_json(
                    {
                        "status": "error",
                        "message": "production create requires explicit confirm=true",
                        "target": "production",
                    },
                    400,
                )
                return

            client = YandexDirectClient.for_target(target)

            try:
                if target == "sandbox":
                    result = client.add_unified_campaign_sandbox(
                        name=data["campaign_name"],
                        start_date=start_date,
                        goal_id=int(data["metrica_goal_id"]),
                        cpa_micros=rub_to_micros(data["target_cpa_rub"]),
                        weekly_budget_micros=rub_to_micros(data["weekly_budget_rub"]),
                        counter_id=DEFAULT_METRICA_COUNTER_ID,
                    )
                else:
                    result = client.add_unified_campaign_production(
                        name=data["campaign_name"],
                        start_date=start_date,
                        goal_id=int(data["metrica_goal_id"]),
                        cpa_micros=rub_to_micros(data["target_cpa_rub"]),
                        weekly_budget_micros=rub_to_micros(data["weekly_budget_rub"]),
                        counter_id=DEFAULT_METRICA_COUNTER_ID,
                    )
            except YandexDirectClientError as e:
                self._send_json(
                    {
                        "status": "error",
                        "message": str(e),
                        "target": target,
                    },
                    502,
                )
                return

            parsed = parse_add_result(result, "campaign")
            if not parsed["ok"]:
                payload = parsed["payload"]
                payload["target"] = target
                self._send_json(payload, parsed["status"])
                return

            success_payload = {
                "status": "success",
                "campaign_id": parsed["payload"]["id"],
                "target": target,
                "start_date": start_date,
                "data": data,
                "applied_defaults": {
                    "metrica_counter_id": DEFAULT_METRICA_COUNTER_ID,
                    "search_placement_types": dict(YandexDirectClient.DEFAULT_SEARCH_PLACEMENT_TYPES),
                    "network_placement_types": dict(YandexDirectClient.DEFAULT_NETWORK_PLACEMENT_TYPES),
                    "time_targeting_sent": False,
                },
            }
            self._send_json(success_payload, parsed["status"])
            return

        if self.path == "/update_campaign":
            target = resolve_target(data)
            if target not in ALLOWED_TARGETS:
                self._send_json(
                    {
                        "status": "error",
                        "message": "target must be 'sandbox' or 'production'",
                    },
                    400,
                )
                return

            campaign_id = normalize_campaign_id(data.get("campaign_id"))
            if campaign_id is None:
                self._send_json(
                    {
                        "status": "error",
                        "message": "campaign_id must be a positive integer or numeric string",
                    },
                    400,
                )
                return

            campaign_name = data.get("campaign_name")
            if campaign_name is not None:
                if not isinstance(campaign_name, str) or not campaign_name.strip():
                    self._send_json(
                        {
                            "status": "error",
                            "message": "campaign_name must be a non-empty string when provided",
                        },
                        400,
                    )
                    return
                campaign_name = campaign_name.strip()

            target_cpa_rub = normalize_positive_number(data.get("target_cpa_rub"))
            weekly_budget_rub = normalize_positive_number(data.get("weekly_budget_rub"))
            metrica_goal_id = normalize_campaign_id(data.get("metrica_goal_id"))

            strategy_update_requested = any(
                key in data for key in ("target_cpa_rub", "weekly_budget_rub", "metrica_goal_id")
            )

            if "target_cpa_rub" in data and target_cpa_rub is None:
                self._send_json(
                    {
                        "status": "error",
                        "message": "target_cpa_rub must be a positive number when provided",
                    },
                    400,
                )
                return

            if "weekly_budget_rub" in data and weekly_budget_rub is None:
                self._send_json(
                    {
                        "status": "error",
                        "message": "weekly_budget_rub must be a positive number when provided",
                    },
                    400,
                )
                return

            if "metrica_goal_id" in data and metrica_goal_id is None:
                self._send_json(
                    {
                        "status": "error",
                        "message": "metrica_goal_id must be a positive integer or numeric string when provided",
                    },
                    400,
                )
                return

            if campaign_name is None and not strategy_update_requested:
                self._send_json(
                    {
                        "status": "error",
                        "message": "nothing to update: provide campaign_name and/or strategy fields",
                    },
                    400,
                )
                return

            confirm = resolve_confirm(data)
            if target == "production" and not confirm:
                self._send_json(
                    {
                        "status": "error",
                        "message": "production campaign update requires explicit confirm=true",
                        "target": "production",
                    },
                    400,
                )
                return

            client = YandexDirectClient.for_target(target)

            try:
                current_goal_id = None
                current_cpa_micros = None
                current_weekly_budget_micros = None

                if strategy_update_requested:
                    current_result = client.get_campaign_details(campaign_id)
                    campaigns = current_result.get("result", {}).get("Campaigns", [])
                    if not campaigns:
                        self._send_json(
                            {
                                "status": "error",
                                "message": "campaign not found",
                                "target": target,
                                "campaign_id": str(campaign_id),
                                "raw": current_result,
                            },
                            404,
                        )
                        return

                    current_strategy = extract_current_upc_strategy(campaigns[0])
                    current_goal_id = current_strategy["goal_id"]
                    current_cpa_micros = current_strategy["cpa_micros"]
                    current_weekly_budget_micros = current_strategy["weekly_budget_micros"]

                    final_goal_id = metrica_goal_id if metrica_goal_id is not None else current_goal_id
                    final_cpa_rub = target_cpa_rub if target_cpa_rub is not None else micros_to_rub(current_cpa_micros)
                    final_weekly_budget_rub = (
                        weekly_budget_rub
                        if weekly_budget_rub is not None
                        else micros_to_rub(current_weekly_budget_micros)
                    )

                    if final_weekly_budget_rub < final_cpa_rub * 20:
                        self._send_json(
                            {
                                "status": "error",
                                "message": (
                                    "weekly_budget_rub must be >= target_cpa_rub * 20 "
                                    f"({final_cpa_rub * 20})"
                                ),
                            },
                            400,
                        )
                        return

                    result = (
                        client.update_campaign_sandbox(
                            campaign_id=campaign_id,
                            name=campaign_name,
                            goal_id=final_goal_id,
                            cpa_micros=rub_to_micros(final_cpa_rub),
                            weekly_budget_micros=rub_to_micros(final_weekly_budget_rub),
                        )
                        if target == "sandbox"
                        else client.update_campaign_production(
                            campaign_id=campaign_id,
                            name=campaign_name,
                            goal_id=final_goal_id,
                            cpa_micros=rub_to_micros(final_cpa_rub),
                            weekly_budget_micros=rub_to_micros(final_weekly_budget_rub),
                        )
                    )
                else:
                    result = (
                        client.update_campaign_sandbox(
                            campaign_id=campaign_id,
                            name=campaign_name,
                        )
                        if target == "sandbox"
                        else client.update_campaign_production(
                            campaign_id=campaign_id,
                            name=campaign_name,
                        )
                    )

            except YandexDirectClientError as e:
                self._send_json(
                    {
                        "status": "error",
                        "message": str(e),
                        "target": target,
                        "campaign_id": str(campaign_id),
                    },
                    502,
                )
                return

            parsed = parse_update_result(result, "campaign")
            if not parsed["ok"]:
                payload = parsed["payload"]
                payload["target"] = target
                payload["campaign_id"] = str(campaign_id)
                self._send_json(payload, parsed["status"])
                return

            response_payload = {
                "status": "success",
                "campaign_id": parsed["payload"]["id"],
                "target": target,
                "warnings": parsed["payload"]["warnings"],
                "result": result,
            }

            if campaign_name is not None:
                response_payload["campaign_name"] = campaign_name

            if strategy_update_requested:
                response_payload["updated_strategy"] = {
                    "metrica_goal_id": (
                        metrica_goal_id if metrica_goal_id is not None else current_goal_id
                    ),
                    "target_cpa_rub": (
                        target_cpa_rub if target_cpa_rub is not None else micros_to_rub(current_cpa_micros)
                    ),
                    "weekly_budget_rub": (
                        weekly_budget_rub
                        if weekly_budget_rub is not None
                        else micros_to_rub(current_weekly_budget_micros)
                    ),
                }

            self._send_json(response_payload, 200)
            return

        if self.path == "/get_campaign_status":
            target = resolve_target(data)
            if target not in ALLOWED_TARGETS:
                self._send_json(
                    {
                        "status": "error",
                        "message": "target must be 'sandbox' or 'production'",
                    },
                    400,
                )
                return

            campaign_id = normalize_campaign_id(data.get("campaign_id"))
            if campaign_id is None:
                self._send_json(
                    {
                        "status": "error",
                        "message": "campaign_id must be a positive integer or numeric string",
                    },
                    400,
                )
                return

            client = YandexDirectClient.for_target(target)

            try:
                result = client.get_campaign_details(campaign_id)
            except YandexDirectClientError as e:
                self._send_json(
                    {
                        "status": "error",
                        "message": str(e),
                        "target": target,
                        "campaign_id": str(campaign_id),
                    },
                    502,
                )
                return

            campaigns = result.get("result", {}).get("Campaigns", [])
            if not campaigns:
                self._send_json(
                    {
                        "status": "error",
                        "message": "campaign not found",
                        "target": target,
                        "campaign_id": str(campaign_id),
                        "raw": result,
                    },
                    404,
                )
                return

            campaign = campaigns[0]

            self._send_json(
                {
                    "status": "success",
                    "target": target,
                    "campaign_id": str(campaign_id),
                    "campaign": campaign,
                },
                200,
            )
            return

        if self.path == "/create_ad_group":
            target = resolve_target(data)
            if target not in ALLOWED_TARGETS:
                self._send_json(
                    {
                        "status": "error",
                        "message": "target must be 'sandbox' or 'production'",
                    },
                    400,
                )
                return

            campaign_id = normalize_campaign_id(data.get("campaign_id"))
            if campaign_id is None:
                self._send_json(
                    {
                        "status": "error",
                        "message": "campaign_id must be a positive integer or numeric string",
                    },
                    400,
                )
                return

            ad_group_name = data.get("ad_group_name")
            if not isinstance(ad_group_name, str) or not ad_group_name.strip():
                self._send_json(
                    {
                        "status": "error",
                        "message": "ad_group_name must be a non-empty string",
                    },
                    400,
                )
                return

            region_ids = normalize_region_ids(data.get("region_ids"))
            if region_ids is None:
                self._send_json(
                    {
                        "status": "error",
                        "message": "region_ids must be a non-empty array of integers or numeric strings",
                    },
                    400,
                )
                return

            offer_retargeting = resolve_offer_retargeting(data)
            if offer_retargeting not in ALLOWED_OFFER_RETARGETING:
                self._send_json(
                    {
                        "status": "error",
                        "message": "offer_retargeting must be 'YES' or 'NO'",
                    },
                    400,
                )
                return

            negative_keywords = normalize_negative_keywords(data.get("negative_keywords"))
            if data.get("negative_keywords") is not None and negative_keywords is None:
                self._send_json(
                    {
                        "status": "error",
                        "message": "negative_keywords must be an array of non-empty strings",
                    },
                    400,
                )
                return

            confirm = resolve_confirm(data)
            if target == "production" and not confirm:
                self._send_json(
                    {
                        "status": "error",
                        "message": "production ad group create requires explicit confirm=true",
                        "target": "production",
                    },
                    400,
                )
                return

            client = YandexDirectClient.for_target(target)

            try:
                if target == "sandbox":
                    result = client.add_unified_ad_group_sandbox(
                        campaign_id=campaign_id,
                        name=ad_group_name.strip(),
                        region_ids=region_ids,
                        offer_retargeting=offer_retargeting,
                        negative_keywords=negative_keywords,
                    )
                else:
                    result = client.add_unified_ad_group_production(
                        campaign_id=campaign_id,
                        name=ad_group_name.strip(),
                        region_ids=region_ids,
                        offer_retargeting=offer_retargeting,
                        negative_keywords=negative_keywords,
                    )
            except YandexDirectClientError as e:
                self._send_json(
                    {
                        "status": "error",
                        "message": str(e),
                        "target": target,
                        "campaign_id": str(campaign_id),
                    },
                    502,
                )
                return

            parsed = parse_add_result(result, "ad_group")
            if not parsed["ok"]:
                payload = parsed["payload"]
                payload["target"] = target
                payload["campaign_id"] = str(campaign_id)
                self._send_json(payload, parsed["status"])
                return

            self._send_json(
                {
                    "status": "success",
                    "ad_group_id": parsed["payload"]["id"],
                    "campaign_id": str(campaign_id),
                    "target": target,
                    "data": data,
                },
                200,
            )
            return

        if self.path == "/get_ad_group_status":
            target = resolve_target(data)
            if target not in ALLOWED_TARGETS:
                self._send_json(
                    {
                        "status": "error",
                        "message": "target must be 'sandbox' or 'production'",
                    },
                    400,
                )
                return

            ad_group_id = normalize_ad_group_id(data.get("ad_group_id"))
            if ad_group_id is None:
                self._send_json(
                    {
                        "status": "error",
                        "message": "ad_group_id must be a positive integer or numeric string",
                    },
                    400,
                )
                return

            client = YandexDirectClient.for_target(target)

            try:
                result = client.get_ad_group_details(ad_group_id)
            except YandexDirectClientError as e:
                self._send_json(
                    {
                        "status": "error",
                        "message": str(e),
                        "target": target,
                        "ad_group_id": str(ad_group_id),
                    },
                    502,
                )
                return

            ad_groups = result.get("result", {}).get("AdGroups", [])
            if not ad_groups:
                self._send_json(
                    {
                        "status": "error",
                        "message": "ad_group not found",
                        "target": target,
                        "ad_group_id": str(ad_group_id),
                        "raw": result,
                    },
                    404,
                )
                return

            ad_group = ad_groups[0]

            self._send_json(
                {
                    "status": "success",
                    "target": target,
                    "ad_group_id": str(ad_group_id),
                    "ad_group": ad_group,
                },
                200,
            )
            return

        if self.path == "/create_ad":
            target = resolve_target(data)
            if target not in ALLOWED_TARGETS:
                self._send_json(
                    {
                        "status": "error",
                        "message": "target must be 'sandbox' or 'production'",
                    },
                    400,
                )
                return

            ad_group_id = normalize_ad_group_id(data.get("ad_group_id"))
            if ad_group_id is None:
                self._send_json(
                    {
                        "status": "error",
                        "message": "ad_group_id must be a positive integer or numeric string",
                    },
                    400,
                )
                return

            title = data.get("title")
            if not isinstance(title, str) or not title.strip():
                self._send_json(
                    {
                        "status": "error",
                        "message": "title must be a non-empty string",
                    },
                    400,
                )
                return

            text = data.get("text")
            if not isinstance(text, str) or not text.strip():
                self._send_json(
                    {
                        "status": "error",
                        "message": "text must be a non-empty string",
                    },
                    400,
                )
                return

            href = data.get("href")
            if not isinstance(href, str) or not href.strip():
                self._send_json(
                    {
                        "status": "error",
                        "message": "href must be a non-empty string",
                    },
                    400,
                )
                return

            display_url_path = data.get("display_url_path")
            if display_url_path is not None:
                if not isinstance(display_url_path, str) or not display_url_path.strip():
                    self._send_json(
                        {
                            "status": "error",
                            "message": "display_url_path must be a non-empty string when provided",
                        },
                        400,
                    )
                    return
                display_url_path = display_url_path.strip()

            confirm = resolve_confirm(data)
            if target == "production" and not confirm:
                self._send_json(
                    {
                        "status": "error",
                        "message": "production ad create requires explicit confirm=true",
                        "target": "production",
                    },
                    400,
                )
                return

            client = YandexDirectClient.for_target(target)

            try:
                if target == "sandbox":
                    result = client.add_text_ad_sandbox(
                        ad_group_id=ad_group_id,
                        title=title.strip(),
                        text=text.strip(),
                        href=href.strip(),
                        display_url_path=display_url_path,
                    )
                else:
                    result = client.add_text_ad_production(
                        ad_group_id=ad_group_id,
                        title=title.strip(),
                        text=text.strip(),
                        href=href.strip(),
                        display_url_path=display_url_path,
                    )
            except YandexDirectClientError as e:
                self._send_json(
                    {
                        "status": "error",
                        "message": str(e),
                        "target": target,
                        "ad_group_id": str(ad_group_id),
                    },
                    502,
                )
                return

            parsed = parse_add_result(result, "ad")
            if not parsed["ok"]:
                payload = parsed["payload"]
                payload["target"] = target
                payload["ad_group_id"] = str(ad_group_id)
                self._send_json(payload, parsed["status"])
                return

            self._send_json(
                {
                    "status": "success",
                    "ad_id": parsed["payload"]["id"],
                    "ad_group_id": str(ad_group_id),
                    "target": target,
                    "data": data,
                },
                200,
            )
            return

        if self.path == "/get_ad_status":
            target = resolve_target(data)
            if target not in ALLOWED_TARGETS:
                self._send_json(
                    {
                        "status": "error",
                        "message": "target must be 'sandbox' or 'production'",
                    },
                    400,
                )
                return

            ad_id = normalize_ad_id(data.get("ad_id"))
            if ad_id is None:
                self._send_json(
                    {
                        "status": "error",
                        "message": "ad_id must be a positive integer or numeric string",
                    },
                    400,
                )
                return

            client = YandexDirectClient.for_target(target)

            try:
                result = client.get_ad_details(ad_id)
            except YandexDirectClientError as e:
                self._send_json(
                    {
                        "status": "error",
                        "message": str(e),
                        "target": target,
                        "ad_id": str(ad_id),
                    },
                    502,
                )
                return

            ads = result.get("result", {}).get("Ads", [])
            if not ads:
                self._send_json(
                    {
                        "status": "error",
                        "message": "ad not found",
                        "target": target,
                        "ad_id": str(ad_id),
                        "raw": result,
                    },
                    404,
                )
                return

            ad = ads[0]

            self._send_json(
                {
                    "status": "success",
                    "target": target,
                    "ad_id": str(ad_id),
                    "ad": ad,
                },
                200,
            )
            return

        if self.path == "/moderate_ad":
            target = resolve_target(data)
            if target not in ALLOWED_TARGETS:
                self._send_json(
                    {
                        "status": "error",
                        "message": "target must be 'sandbox' or 'production'",
                    },
                    400,
                )
                return

            ad_id = normalize_ad_id(data.get("ad_id"))
            if ad_id is None:
                self._send_json(
                    {
                        "status": "error",
                        "message": "ad_id must be a positive integer or numeric string",
                    },
                    400,
                )
                return

            confirm = resolve_confirm(data)
            if target == "production" and not confirm:
                self._send_json(
                    {
                        "status": "error",
                        "message": "production ad moderation requires explicit confirm=true",
                        "target": "production",
                    },
                    400,
                )
                return

            client = YandexDirectClient.for_target(target)

            try:
                if target == "sandbox":
                    result = client.moderate_ads_sandbox([ad_id])
                else:
                    result = client.moderate_ads_production([ad_id])
            except YandexDirectClientError as e:
                self._send_json(
                    {
                        "status": "error",
                        "message": str(e),
                        "target": target,
                        "ad_id": str(ad_id),
                    },
                    502,
                )
                return

            self._send_json(
                {
                    "status": "success",
                    "target": target,
                    "ad_id": str(ad_id),
                    "result": result,
                },
                200,
            )
            return

        self._send_json({"status": "error", "message": "not found"}, 404)


def main():
    server = HTTPServer((settings.host, settings.port), Handler)
    print(f"Backend listening on http://{settings.host}:{settings.port}")
    server.serve_forever()


if __name__ == "__main__":
    main()