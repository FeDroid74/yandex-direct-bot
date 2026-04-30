import base64
import binascii
from datetime import date, datetime, timedelta
from html.parser import HTMLParser
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import mimetypes
import os
import re
import urllib.error
import urllib.parse
import urllib.request
import uuid
from typing import List, Optional

from config import settings
from services.direct_client import YandexDirectClient, YandexDirectClientError
from services.direct_mock import validate_campaign
from services.search_terms_analyzer import analyze_search_terms, build_negative_preview_candidates


ALLOWED_TARGETS = {"sandbox", "production"}
ALLOWED_OFFER_RETARGETING = {"YES", "NO"}
ALLOWED_AUTOTARGETING_SETTINGS_VALUES = {"YES", "NO"}
ALLOWED_AUTOTARGETING_CATEGORY_KEYS = {"Exact", "Narrow", "Alternative", "Accessory", "Broader"}
ALLOWED_AUTOTARGETING_BRAND_OPTION_KEYS = {"WithoutBrands", "WithAdvertiserBrand", "WithCompetitorsBrand"}
DEFAULT_METRICA_COUNTER_ID = 99041859
DEFAULT_GOAL_ID = 352606262
DEFAULT_PRODUCTION_REGION_IDS = [225]
STATE_FILE_PATH = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "STATE.md"))
ARTFARFOR_BASE_URL = "https://artfarfor.com/"
ARTFARFOR_DOMAIN = "artfarfor.com"
ARTFARFOR_IMAGE_ALLOWED_DOMAINS = {ARTFARFOR_DOMAIN, "static.insales-cdn.com"}
DEFAULT_COMPETITOR_ANALYSIS_URLS = [
    "https://farforts.ru/",
    "https://starivina.ru/",
    "https://kunstgalerie.ru/",
]
ALLOWED_COMPETITOR_DOMAINS = {"farforts.ru", "starivina.ru", "kunstgalerie.ru"}
DEFAULT_SITE_IMAGE_LIMIT = 3
MAX_SITE_DISCOVERY_PAGES = 18
MAX_SITE_LINKS_PER_PAGE = 20
MAX_COMPETITOR_ANALYSIS_PAGES = 16
MAX_COMPETITOR_ANALYSIS_DEPTH = 3
MAX_COMPETITOR_LINKS_PER_PAGE = 14
SITE_HTTP_TIMEOUT_SECONDS = 20
SITE_THEME_STOPWORDS = {"и", "или", "для", "на", "в", "с", "к", "по", "из", "а", "the"}
DECORATIVE_IMAGE_HINTS = (
    "logo",
    "favicon",
    "icon",
    "sprite",
    "banner",
    "no_image",
    "payment",
    "telegram",
    "whatsapp",
    "vk",
    "loader",
    "placeholder",
)
CRAWL_PATH_BANNED_HINTS = ("/contacts", "/delivery", "/oplata", "/cart", "/profile", "/login")
COMPETITOR_CRAWL_BANNED_HINTS = CRAWL_PATH_BANNED_HINTS + (
    "/contact",
    "/payment",
    "/policy",
    "/privacy",
    "/checkout",
    "/wishlist",
    "/register",
)
COMPETITOR_PATH_POSITIVE_HINTS = (
    "/catalog",
    "/category",
    "/product",
    "/component",
    "/shop",
    "/item",
    "/figur",
    "/statu",
    "/farfor",
    "/kloun",
    "/pier",
    "/arlek",
)
COMPETITOR_POSITIONING_HINTS = (
    "фарфор",
    "статуэт",
    "фигур",
    "антик",
    "винтаж",
    "подар",
    "интерьер",
    "коллекц",
)
SUPPORTED_SESSION_MODES = [
    "draft_campaign",
    "review_draft",
    "analyze_campaign",
    "awaiting_confirm_create",
    "awaiting_confirm_update",
]
DEFAULT_DRAFT_TRACKING_PARAMS = (
    "utm_source=yandex&utm_medium=cpc&utm_campaign={campaign_id}&utm_content={ad_id}&utm_term={keyword}"
)


def rub_to_micros(value_rub: float) -> int:
    return int(float(value_rub) * 1_000_000)


def micros_to_rub(value_micros: int) -> float:
    return float(value_micros) / 1_000_000


def format_rub_value(value):
    numeric = float(value)
    if numeric.is_integer():
        return int(numeric)
    return numeric


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


def normalize_positive_int_id(value):
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


def normalize_campaign_id(value):
    return normalize_positive_int_id(value)


def normalize_ad_group_id(value):
    return normalize_positive_int_id(value)


def normalize_ad_id(value):
    return normalize_positive_int_id(value)


def normalize_non_negative_int(value):
    if isinstance(value, bool):
        return None

    if isinstance(value, int):
        return value if value >= 0 else None

    if isinstance(value, str):
        stripped = value.strip()
        if not stripped.isdigit():
            return None
        return int(stripped)

    return None


def normalize_non_empty_string(value) -> Optional[str]:
    if not isinstance(value, str):
        return None

    stripped = value.strip()
    if not stripped:
        return None

    return stripped


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


def safe_float(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def enrich_metrics(rows):
    enriched = []

    for row in rows:
        clicks = safe_float(row.get("Clicks"))
        impressions = safe_float(row.get("Impressions"))
        cost = safe_float(row.get("Cost"))
        all_goals_conversions = safe_float(row.get("Conversions"))

        ctr = (clicks / impressions * 100) if impressions > 0 else 0.0
        cpc = (cost / clicks) if clicks > 0 else 0.0

        new_row = dict(row)
        new_row["AllGoalsConversions"] = new_row.pop("Conversions", "0")
        new_row["AllGoalsConversionRate"] = new_row.pop("ConversionRate", None)
        new_row["AllGoalsCostPerConversion"] = new_row.pop("CostPerConversion", None)
        new_row.update({
            "CTR": round(ctr, 4),
            "CPC": round(cpc, 4),
            "GoalCPA": None,
            "GoalConversionsConfirmed": False,
            "AllGoalsConversionsNumeric": all_goals_conversions,
        })

        enriched.append(new_row)

    return enriched


def normalize_iso_date(value) -> Optional[str]:
    if not isinstance(value, str):
        return None

    stripped = value.strip()
    if not stripped:
        return None

    try:
        parsed = date.fromisoformat(stripped)
    except ValueError:
        return None

    return parsed.isoformat()


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


def normalize_autotargeting_settings(value):
    if not isinstance(value, dict):
        return None

    normalized = {}

    if "Categories" in value:
        categories = value.get("Categories")
        if not isinstance(categories, dict) or not categories:
            return None

        normalized_categories = {}
        for key, raw_setting_value in categories.items():
            if key not in ALLOWED_AUTOTARGETING_CATEGORY_KEYS:
                return None
            if not isinstance(raw_setting_value, str):
                return None
            setting_value = raw_setting_value.strip().upper()
            if setting_value not in ALLOWED_AUTOTARGETING_SETTINGS_VALUES:
                return None
            normalized_categories[key] = setting_value

        normalized["Categories"] = normalized_categories

    if "BrandOptions" in value:
        brand_options = value.get("BrandOptions")
        if not isinstance(brand_options, dict) or not brand_options:
            return None

        normalized_brand_options = {}
        for key, raw_setting_value in brand_options.items():
            if key not in ALLOWED_AUTOTARGETING_BRAND_OPTION_KEYS:
                return None
            if not isinstance(raw_setting_value, str):
                return None
            setting_value = raw_setting_value.strip().upper()
            if setting_value not in ALLOWED_AUTOTARGETING_SETTINGS_VALUES:
                return None
            normalized_brand_options[key] = setting_value

        normalized["BrandOptions"] = normalized_brand_options

    if not normalized:
        return None

    return normalized


def deep_copy_json(value):
    return json.loads(json.dumps(value, ensure_ascii=False))


def build_default_approved_patterns() -> dict:
    return {
        "negative_keywords_defaults": None,
        "sitelinks_defaults": None,
        "autotargeting_defaults": None,
        "campaign_name_suffix": None,
        "budget_defaults": None,
    }


def build_default_draft_autotargeting_settings():
    return {
        "Categories": {
            "Exact": "YES",
            "Narrow": "YES",
            "Alternative": "YES",
            "Accessory": "YES",
            "Broader": "YES",
        },
        "BrandOptions": {
            "WithoutBrands": "YES",
            "WithAdvertiserBrand": "YES",
            "WithCompetitorsBrand": "NO",
        },
    }


def normalize_sitelinks_defaults(value):
    if not isinstance(value, list) or not value:
        return None

    normalized = []
    for item in value:
        if not isinstance(item, dict):
            return None

        title = item.get("title")
        href = item.get("href")

        if not isinstance(title, str) or not title.strip():
            return None
        if not isinstance(href, str) or not href.strip():
            return None
        if not href.strip().startswith("https://artfarfor.com"):
            return None

        normalized.append(
            {
                "title": title.strip(),
                "href": href.strip(),
            }
        )

    return normalized


def normalize_budget_defaults(value):
    if not isinstance(value, dict) or not value:
        return None

    allowed_keys = {"weekly_budget_rub", "target_cpa_rub"}
    if any(key not in allowed_keys for key in value.keys()):
        return None

    normalized = {}

    if "weekly_budget_rub" in value:
        weekly_budget_rub = normalize_positive_number(value.get("weekly_budget_rub"))
        if weekly_budget_rub is None:
            return None
        if weekly_budget_rub > 300000:
            return None
        normalized["weekly_budget_rub"] = format_rub_value(weekly_budget_rub)

    if "target_cpa_rub" in value:
        target_cpa_rub = normalize_positive_number(value.get("target_cpa_rub"))
        if target_cpa_rub is None:
            return None
        if target_cpa_rub > 30000:
            return None
        normalized["target_cpa_rub"] = format_rub_value(target_cpa_rub)

    if not normalized:
        return None

    if (
        "weekly_budget_rub" in normalized
        and "target_cpa_rub" in normalized
        and normalized["weekly_budget_rub"] < normalized["target_cpa_rub"] * 20
    ):
        return None

    return normalized


def merge_autotargeting_settings(base_settings: dict, override_settings: dict) -> dict:
    merged = deep_copy_json(base_settings)

    if "Categories" in override_settings:
        merged.setdefault("Categories", {})
        merged["Categories"].update(override_settings["Categories"])

    if "BrandOptions" in override_settings:
        merged.setdefault("BrandOptions", {})
        merged["BrandOptions"].update(override_settings["BrandOptions"])

    return merged


def build_ad_group_draft(name: str) -> dict:
    group_name = name.strip()
    return {
        "group_name": group_name,
        "negative_keywords": [],
        "autotargeting_settings": build_default_draft_autotargeting_settings(),
        "ads": [
            {
                "title": f"{group_name} | ArtFarfor",
                "text": "Авторские фарфоровые статуэтки и подарки ручной работы",
                "final_url": "https://artfarfor.com",
                "ad_image_hash": None,
                "ad_image_hashes": [],
                "creative_spec": None,
            }
        ],
    }


def build_draft_ad_fragment(title_seed: str, text: str) -> dict:
    normalized_title_seed = title_seed.strip()
    normalized_text = text.strip()
    return {
        "title": f"{normalized_title_seed} | ArtFarfor",
        "text": normalized_text,
        "final_url": "https://artfarfor.com",
        "ad_image_hash": None,
        "ad_image_hashes": [],
        "creative_spec": None,
    }


def build_rebuild_group_name(theme: str, group_index: int) -> str:
    normalized_theme = theme.strip()
    group_suffixes = [
        "",
        " в подарок",
        " для интерьера",
        " для коллекции",
        " ручной работы",
        " для дома",
        " для декора",
        " для ценителей",
    ]
    if group_index < len(group_suffixes):
        return f"{normalized_theme}{group_suffixes[group_index]}".strip()
    return f"{normalized_theme} {group_index + 1}"


def build_rebuild_ad_text(theme: str, group_name: str, ad_index: int) -> str:
    normalized_theme = theme.strip()
    normalized_group_name = group_name.strip()
    text_variants = [
        f"{normalized_group_name} из фарфора для интерьера, коллекции и подарка.",
        f"{normalized_group_name} ручной работы для дома, витрины и коллекции.",
        f"{normalized_theme} для подарка и декора с доставкой по России.",
        f"{normalized_theme} для ценителей авторского и коллекционного фарфора.",
        f"{normalized_group_name} для подарка, интерьера и домашней коллекции.",
        f"{normalized_theme} для дома и подарка тем, кто любит фарфоровый декор.",
    ]
    return text_variants[ad_index % len(text_variants)]


def build_rebuild_ad_group_draft(theme: str, group_index: int, ads_per_group: int) -> dict:
    group_name = build_rebuild_group_name(theme, group_index)
    ads = []
    for ad_index in range(ads_per_group):
        ads.append(
            build_draft_ad_fragment(
                group_name,
                build_rebuild_ad_text(theme, group_name, ad_index),
            )
        )

    return {
        "group_name": group_name,
        "negative_keywords": [],
        "autotargeting_settings": build_default_draft_autotargeting_settings(),
        "ads": ads,
    }


def build_rebuild_campaign_ads(theme: str, ads_count: int) -> list[dict]:
    normalized_theme = theme.strip()
    ads = []
    for ad_index in range(ads_count):
        title_seed = build_rebuild_group_name(normalized_theme, ad_index)
        ads.append(
            build_draft_ad_fragment(
                title_seed,
                build_rebuild_ad_text(normalized_theme, title_seed, ad_index),
            )
        )
    return ads


def build_rebuild_draft_campaign(theme: str, ad_groups_count: int = 4, ads_per_group: int = 4) -> dict:
    normalized_theme = theme.strip()
    ad_groups = []
    for group_index in range(ad_groups_count):
        ad_groups.append(build_rebuild_ad_group_draft(normalized_theme, group_index, ads_per_group))

    return {
        "campaign_type": "UNIFIED_CAMPAIGN",
        "campaign_name": f"{normalized_theme} | ArtFarfor",
        "site_url": "https://artfarfor.com",
        "region": "RU",
        "language": "RU",
        "placement_type": "both",
        "goal_type": "leads",
        "strategy_type": "pay_for_conversion",
        "metrica_goal_id": DEFAULT_GOAL_ID,
        "metrica_counter_id": DEFAULT_METRICA_COUNTER_ID,
        "weekly_budget_rub": 10000,
        "target_cpa_rub": 500,
        "tracking_params": DEFAULT_DRAFT_TRACKING_PARAMS,
        "utm_tracking": True,
        "negative_keywords": ["бесплатно", "дешево"],
        "sitelinks": [
            {
                "title": "Каталог",
                "href": "https://artfarfor.com",
            },
            {
                "title": "Коллекции",
                "href": "https://artfarfor.com",
            },
        ],
        "ad_groups": ad_groups,
        "ads": build_rebuild_campaign_ads(normalized_theme, ads_per_group),
        "autotargeting_settings": build_default_draft_autotargeting_settings(),
        "assumptions": [
            "Assumption: used fixed site_url https://artfarfor.com per project restriction.",
            "Assumption: region='RU' and goal_type='leads' are draft defaults for backend validate/create flow and require review.",
            "Assumption: used RU, both placements, pay_for_conversion, metrica_goal_id 352606262 per confirmed project constraints.",
            "Assumption: weekly_budget_rub=10000 and target_cpa_rub=500 are draft defaults and require review.",
            "Assumption: sitelinks and negative keywords are prototype placeholders for review_draft.",
        ],
    }


def build_draft_campaign_from_theme(theme: str) -> dict:
    normalized_theme = theme.strip()
    return {
        "campaign_type": "UNIFIED_CAMPAIGN",
        "campaign_name": f"{normalized_theme} | ArtFarfor",
        "site_url": "https://artfarfor.com",
        "region": "RU",
        "language": "RU",
        "placement_type": "both",
        "goal_type": "leads",
        "strategy_type": "pay_for_conversion",
        "metrica_goal_id": DEFAULT_GOAL_ID,
        "metrica_counter_id": DEFAULT_METRICA_COUNTER_ID,
        "weekly_budget_rub": 10000,
        "target_cpa_rub": 500,
        "tracking_params": DEFAULT_DRAFT_TRACKING_PARAMS,
        "utm_tracking": True,
        "negative_keywords": ["бесплатно", "дешево"],
        "sitelinks": [
            {
                "title": "Каталог",
                "href": "https://artfarfor.com",
            },
            {
                "title": "Коллекции",
                "href": "https://artfarfor.com",
            },
        ],
        "ad_groups": [
            build_ad_group_draft(normalized_theme),
        ],
        "ads": [
            {
                "title": f"{normalized_theme} | ArtFarfor",
                "text": "Подарочные фарфоровые статуэтки ручной работы",
                "final_url": "https://artfarfor.com",
                "ad_image_hash": None,
                "ad_image_hashes": [],
                "creative_spec": None,
            }
        ],
        "autotargeting_settings": build_default_draft_autotargeting_settings(),
        "assumptions": [
            "Assumption: used fixed site_url https://artfarfor.com per project restriction.",
            "Assumption: region='RU' and goal_type='leads' are draft defaults for backend validate/create flow and require review.",
            "Assumption: used RU, both placements, pay_for_conversion, metrica_goal_id 352606262 per confirmed project constraints.",
            "Assumption: weekly_budget_rub=10000 and target_cpa_rub=500 are draft defaults and require review.",
            "Assumption: sitelinks and negative keywords are prototype placeholders for review_draft.",
        ],
    }


def apply_approved_patterns_to_draft(theme: str, draft_campaign: dict, approved_patterns: dict) -> dict:
    draft = deep_copy_json(draft_campaign)
    assumptions = draft.setdefault("assumptions", [])

    if not isinstance(approved_patterns, dict):
        return draft

    campaign_name_suffix = approved_patterns.get("campaign_name_suffix")
    if isinstance(campaign_name_suffix, str) and campaign_name_suffix:
        draft["campaign_name"] = f"{theme.strip()}{campaign_name_suffix}"

    negative_keywords_defaults = approved_patterns.get("negative_keywords_defaults")
    if isinstance(negative_keywords_defaults, list) and negative_keywords_defaults:
        draft["negative_keywords"] = deep_copy_json(negative_keywords_defaults)

    sitelinks_defaults = approved_patterns.get("sitelinks_defaults")
    if isinstance(sitelinks_defaults, list) and sitelinks_defaults:
        allowed_sitelinks = []
        skipped_sitelinks = False
        for sitelink in sitelinks_defaults:
            href = sitelink.get("href")
            if isinstance(href, str) and href.startswith("https://artfarfor.com"):
                allowed_sitelinks.append(deep_copy_json(sitelink))
                continue
            skipped_sitelinks = True

        if allowed_sitelinks:
            draft["sitelinks"] = allowed_sitelinks
        if skipped_sitelinks:
            assumptions.append("Assumption: skipped sitelinks_defaults entries outside https://artfarfor.com.")

    autotargeting_defaults = approved_patterns.get("autotargeting_defaults")
    if isinstance(autotargeting_defaults, dict) and autotargeting_defaults:
        draft["autotargeting_settings"] = merge_autotargeting_settings(
            draft.get("autotargeting_settings", build_default_draft_autotargeting_settings()),
            autotargeting_defaults,
        )
        for ad_group in draft.get("ad_groups", []):
            if isinstance(ad_group, dict):
                ad_group["autotargeting_settings"] = merge_autotargeting_settings(
                    ad_group.get("autotargeting_settings", build_default_draft_autotargeting_settings()),
                    autotargeting_defaults,
                )

    budget_defaults = approved_patterns.get("budget_defaults")
    if isinstance(budget_defaults, dict) and budget_defaults:
        proposed_weekly_budget = budget_defaults.get("weekly_budget_rub", draft.get("weekly_budget_rub"))
        proposed_target_cpa = budget_defaults.get("target_cpa_rub", draft.get("target_cpa_rub"))

        if (
            isinstance(proposed_weekly_budget, (int, float))
            and isinstance(proposed_target_cpa, (int, float))
            and proposed_weekly_budget >= proposed_target_cpa * 20
        ):
            if "weekly_budget_rub" in budget_defaults:
                draft["weekly_budget_rub"] = budget_defaults["weekly_budget_rub"]
            if "target_cpa_rub" in budget_defaults:
                draft["target_cpa_rub"] = budget_defaults["target_cpa_rub"]
        else:
            assumptions.append("Assumption: skipped budget_defaults because it violates weekly_budget_rub >= target_cpa_rub * 20.")

    return draft


def build_default_runtime_state() -> dict:
    return {
        "session_mode": "draft_campaign",
        "draft_campaign": None,
        "campaign_payload": None,
        "validation_result": None,
        "created_campaign_id": None,
        "last_plan": None,
        "last_proposal": None,
        "proposal_history": [],
        "approved_patterns": build_default_approved_patterns(),
        "media_library": [],
        "draft_media": [],
        "last_uploaded_image_hash": None,
        "creative_spec": None,
        "render_task": None,
        "render_result_url": None,
        "draft_meta": {
            "version": "v1",
            "last_action": None,
            "theme": None,
            "revision_count": 0,
            "supported_revision_rules": [
                "измени название кампании на X",
                "добавь группу X",
                "добавь минус-слово X",
            ],
        },
    }


def render_state_markdown(state: dict) -> str:
    rendered_state = json.dumps(state, ensure_ascii=False, indent=2)
    return (
        "# STATE.md - Campaign Draft State\n\n"
        "## Session Modes\n\n"
        "- draft_campaign\n"
        "- review_draft\n"
        "- analyze_campaign\n"
        "- awaiting_confirm_create\n"
        "- awaiting_confirm_update\n\n"
        "## Current State\n\n"
        "```json\n"
        f"{rendered_state}\n"
        "```\n"
    )


def load_runtime_state() -> dict:
    if not os.path.exists(STATE_FILE_PATH):
        return build_default_runtime_state()

    try:
        with open(STATE_FILE_PATH, "r", encoding="utf-8") as state_file:
            raw_state = state_file.read()
    except OSError:
        return build_default_runtime_state()

    marker = "```json"
    start = raw_state.find(marker)
    if start == -1:
        return build_default_runtime_state()

    start += len(marker)
    end = raw_state.find("```", start)
    if end == -1:
        return build_default_runtime_state()

    json_block = raw_state[start:end].strip()
    if not json_block:
        return build_default_runtime_state()

    try:
        parsed = json.loads(json_block)
    except json.JSONDecodeError:
        return build_default_runtime_state()

    if not isinstance(parsed, dict):
        return build_default_runtime_state()

    state = build_default_runtime_state()
    state.update(
        {
            key: parsed.get(key)
            for key in (
                "session_mode",
                "draft_campaign",
                "campaign_payload",
                "validation_result",
                "created_campaign_id",
                "last_plan",
                "last_proposal",
                "proposal_history",
                "approved_patterns",
                "media_library",
                "draft_media",
                "last_uploaded_image_hash",
                "creative_spec",
                "render_task",
                "render_result_url",
                "draft_meta",
            )
        }
    )
    if state.get("draft_meta") is None:
        state["draft_meta"] = build_default_runtime_state()["draft_meta"]
    if not isinstance(state.get("proposal_history"), list):
        state["proposal_history"] = []
    if not isinstance(state.get("approved_patterns"), dict):
        state["approved_patterns"] = build_default_approved_patterns()
    if not isinstance(state.get("media_library"), list):
        state["media_library"] = []
    if not isinstance(state.get("draft_media"), list):
        state["draft_media"] = []
    return state


RUNTIME_STATE = load_runtime_state()


def save_runtime_state(state: dict) -> None:
    global RUNTIME_STATE
    normalize_draft_campaign_images(state.get("draft_campaign"))
    RUNTIME_STATE = deep_copy_json(state)
    with open(STATE_FILE_PATH, "w", encoding="utf-8") as state_file:
        state_file.write(render_state_markdown(RUNTIME_STATE))


def extract_media_state(state: dict) -> dict:
    media_library = state.get("media_library")
    draft_media = state.get("draft_media")

    return {
        "media_library": deep_copy_json(media_library) if isinstance(media_library, list) else [],
        "draft_media": deep_copy_json(draft_media) if isinstance(draft_media, list) else [],
        "last_uploaded_image_hash": state.get("last_uploaded_image_hash"),
        "creative_spec": deep_copy_json(state.get("creative_spec")),
        "render_task": deep_copy_json(state.get("render_task")),
        "render_result_url": state.get("render_result_url"),
    }


def build_multipart_file_body(filename: str, file_bytes: bytes, field_name: str = "file", content_type: Optional[str] = None):
    boundary = f"----CodexBoundary{uuid.uuid4().hex}"
    resolved_content_type = normalize_non_empty_string(content_type) or mimetypes.guess_type(filename)[0] or "application/octet-stream"
    body = b"".join(
        [
            f"--{boundary}\r\n".encode("utf-8"),
            f'Content-Disposition: form-data; name="{field_name}"; filename="{filename}"\r\n'.encode("utf-8"),
            f"Content-Type: {resolved_content_type}\r\n\r\n".encode("utf-8"),
            file_bytes,
            f"\r\n--{boundary}--\r\n".encode("utf-8"),
        ]
    )
    return body, f"multipart/form-data; boundary={boundary}"


def upload_image_to_supa_api(filename: str, image_bytes: bytes, content_type: Optional[str] = None) -> dict:
    body, multipart_content_type = build_multipart_file_body(
        filename=filename,
        file_bytes=image_bytes,
        content_type=content_type,
    )

    request = urllib.request.Request(
        settings.supa_upload_url,
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bearer {settings.supa_api_key}",
            "Content-Type": multipart_content_type,
            "Accept": "application/json",
        },
    )

    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            raw_body = response.read().decode("utf-8", errors="replace")
            status_code = getattr(response, "status", 200)
    except urllib.error.HTTPError as error:
        raw_body = error.read().decode("utf-8", errors="replace")
        try:
            payload = json.loads(raw_body) if raw_body else {"message": error.reason}
        except json.JSONDecodeError:
            payload = {"raw": raw_body or str(error.reason)}
        return {"ok": False, "status_code": error.code, "payload": payload}
    except urllib.error.URLError as error:
        return {"ok": False, "status_code": 502, "payload": {"message": str(error.reason)}}

    try:
        payload = json.loads(raw_body)
    except json.JSONDecodeError:
        payload = {"raw": raw_body}

    if not isinstance(payload, dict):
        payload = {"raw": raw_body}

    return {"ok": 200 <= status_code < 300, "status_code": status_code, "payload": payload}


class ArtFarforHTMLParser(HTMLParser):
    def __init__(self, base_url: str):
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self.title_parts = []
        self.heading_parts = []
        self.text_parts = []
        self.images = []
        self.links = []
        self.meta_image_urls = []
        self._skip_depth = 0
        self._current_link = None
        self._current_heading_tag = None
        self._current_heading_parts = []
        self._in_title = False

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        lower_tag = tag.lower()

        if lower_tag in {"script", "style", "noscript"}:
            self._skip_depth += 1
            return

        if lower_tag == "title":
            self._in_title = True
            return

        if lower_tag in {"h1", "h2", "h3"}:
            self._current_heading_tag = lower_tag
            self._current_heading_parts = []
            return

        if lower_tag == "a":
            href = attributes.get("href")
            if href:
                self._current_link = {"href": href, "text_parts": []}
            return

        if lower_tag == "img":
            src = attributes.get("src") or attributes.get("data-src") or attributes.get("data-original")
            if src:
                self.images.append(
                    {
                        "url": urllib.parse.urljoin(self.base_url, src),
                        "alt": clean_inline_text(attributes.get("alt", "")),
                        "class": clean_inline_text(attributes.get("class", "")),
                    }
                )
            return

        if lower_tag == "meta":
            meta_key = (attributes.get("property") or attributes.get("name") or "").strip().lower()
            content = attributes.get("content") or ""
            if meta_key == "og:image" and content:
                self.meta_image_urls.append(urllib.parse.urljoin(self.base_url, content))

    def handle_endtag(self, tag):
        lower_tag = tag.lower()

        if lower_tag in {"script", "style", "noscript"}:
            if self._skip_depth > 0:
                self._skip_depth -= 1
            return

        if lower_tag == "title":
            self._in_title = False
            return

        if lower_tag in {"h1", "h2", "h3"} and self._current_heading_tag == lower_tag:
            heading_text = clean_inline_text(" ".join(self._current_heading_parts))
            if heading_text:
                self.heading_parts.append(heading_text)
            self._current_heading_tag = None
            self._current_heading_parts = []
            return

        if lower_tag == "a" and self._current_link is not None:
            link_text = clean_inline_text(" ".join(self._current_link["text_parts"]))
            self.links.append(
                {
                    "url": urllib.parse.urljoin(self.base_url, self._current_link["href"]),
                    "text": link_text,
                }
            )
            self._current_link = None

    def handle_data(self, data):
        if self._skip_depth > 0:
            return

        text = clean_inline_text(data)
        if not text:
            return

        self.text_parts.append(text)

        if self._in_title:
            self.title_parts.append(text)

        if self._current_heading_tag is not None:
            self._current_heading_parts.append(text)

        if self._current_link is not None:
            self._current_link["text_parts"].append(text)


def clean_inline_text(value) -> str:
    if not isinstance(value, str):
        return ""
    return re.sub(r"\s+", " ", value.replace("\xa0", " ")).strip()


def normalize_match_text(value) -> str:
    return clean_inline_text(value).lower().replace("ё", "е")


def tokenize_match_text(value) -> list:
    return [
        token
        for token in re.findall(r"[0-9a-zа-я]+", normalize_match_text(value))
        if len(token) > 1 and token not in SITE_THEME_STOPWORDS
    ]


def is_artfarfor_domain_url(url: str) -> bool:
    if not isinstance(url, str) or not url.strip():
        return False
    parsed = urllib.parse.urlsplit(url)
    host = (parsed.netloc or "").split("@")[-1].split(":")[0].lower()
    return bool(host) and (host == ARTFARFOR_DOMAIN or host.endswith(f".{ARTFARFOR_DOMAIN}"))


def is_allowed_site_image_domain_url(url: str) -> bool:
    if not isinstance(url, str) or not url.strip():
        return False
    parsed = urllib.parse.urlsplit(url)
    host = (parsed.netloc or "").split("@")[-1].split(":")[0].lower()
    return bool(host) and host in ARTFARFOR_IMAGE_ALLOWED_DOMAINS


def normalize_artfarfor_url(url: str, base_url: str = ARTFARFOR_BASE_URL) -> Optional[str]:
    if not isinstance(url, str) or not url.strip():
        return None
    resolved = urllib.parse.urljoin(base_url, url.strip())
    parsed = urllib.parse.urlsplit(resolved)
    if parsed.scheme not in {"http", "https"}:
        return None
    if not is_artfarfor_domain_url(resolved):
        return None
    normalized_path = parsed.path or "/"
    return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc.lower(), normalized_path, "", ""))


def normalize_site_image_url(url: str, base_url: str = ARTFARFOR_BASE_URL) -> Optional[str]:
    if not isinstance(url, str) or not url.strip():
        return None
    resolved = urllib.parse.urljoin(base_url, url.strip())
    parsed = urllib.parse.urlsplit(resolved)
    if parsed.scheme not in {"http", "https"}:
        return None
    if not is_allowed_site_image_domain_url(resolved):
        return None
    normalized_path = parsed.path or "/"
    return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc.lower(), normalized_path, "", ""))


def is_crawl_candidate_url(url: str) -> bool:
    normalized = normalize_artfarfor_url(url)
    if normalized is None:
        return False
    path = urllib.parse.urlsplit(normalized).path.lower()
    if any(hint in path for hint in CRAWL_PATH_BANNED_HINTS):
        return False
    return path == "/" or path.startswith("/collection") or path.startswith("/product")


def fetch_site_url(url: str, binary: bool = False) -> dict:
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 (compatible; OpenClawSiteImageBot/1.0)",
            "Accept": "*/*" if binary else "text/html,application/xhtml+xml",
        },
    )

    try:
        with urllib.request.urlopen(request, timeout=SITE_HTTP_TIMEOUT_SECONDS) as response:
            body = response.read()
            content_type = response.headers.get("Content-Type", "")
            charset = response.headers.get_content_charset() or "utf-8"
    except urllib.error.HTTPError as error:
        raw_body = error.read().decode("utf-8", errors="replace")
        return {"ok": False, "status_code": error.code, "payload": {"message": str(error.reason), "raw": raw_body}}
    except urllib.error.URLError as error:
        return {"ok": False, "status_code": 502, "payload": {"message": str(error.reason)}}

    if binary:
        return {"ok": True, "status_code": 200, "content_type": content_type, "body": body}

    try:
        text = body.decode(charset, errors="replace")
    except LookupError:
        text = body.decode("utf-8", errors="replace")

    return {"ok": True, "status_code": 200, "content_type": content_type, "text": text}


def parse_artfarfor_page(url: str, html_text: str) -> dict:
    parser = ArtFarforHTMLParser(url)
    parser.feed(html_text)
    parser.close()
    return {
        "url": url,
        "title": clean_inline_text(" ".join(parser.title_parts)),
        "headings": [item for item in parser.heading_parts if item],
        "text": clean_inline_text(" ".join(parser.text_parts)),
        "images": parser.images,
        "meta_image_urls": parser.meta_image_urls,
        "links": parser.links,
    }


def normalize_competitor_text(value) -> str:
    if not isinstance(value, str):
        return ""
    return clean_inline_text(value).lower().replace("ё", "е")


def extract_url_host(url: str) -> str:
    if not isinstance(url, str) or not url.strip():
        return ""
    parsed = urllib.parse.urlsplit(url)
    return (parsed.netloc or "").split("@")[-1].split(":")[0].lower()


def is_allowed_competitor_domain_url(url: str) -> bool:
    host = extract_url_host(url)
    if not host:
        return False
    return host in ALLOWED_COMPETITOR_DOMAINS or any(host.endswith(f".{domain}") for domain in ALLOWED_COMPETITOR_DOMAINS)


def normalize_competitor_base_url(url: str) -> Optional[str]:
    if not isinstance(url, str) or not url.strip():
        return None
    parsed = urllib.parse.urlsplit(url.strip())
    if parsed.scheme not in {"http", "https"}:
        return None
    if not is_allowed_competitor_domain_url(url.strip()):
        return None
    host = extract_url_host(url.strip())
    return urllib.parse.urlunsplit((parsed.scheme, host, "/", "", ""))


def normalize_competitor_page_url(url: str, base_url: str) -> Optional[str]:
    if not isinstance(url, str) or not url.strip():
        return None
    resolved = urllib.parse.urljoin(base_url, url.strip())
    parsed = urllib.parse.urlsplit(resolved)
    if parsed.scheme not in {"http", "https"}:
        return None
    if not is_allowed_competitor_domain_url(resolved):
        return None
    normalized_path = parsed.path or "/"
    return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc.lower(), normalized_path, "", ""))


def is_same_competitor_host(url: str, base_url: str) -> bool:
    return extract_url_host(url) == extract_url_host(base_url)


def is_competitor_crawl_candidate_url(url: str, base_url: str) -> bool:
    normalized = normalize_competitor_page_url(url, base_url)
    if normalized is None or not is_same_competitor_host(normalized, base_url):
        return False
    path = urllib.parse.urlsplit(normalized).path.lower()
    if any(hint in path for hint in COMPETITOR_CRAWL_BANNED_HINTS):
        return False
    if re.search(r"\.(?:jpg|jpeg|png|gif|svg|css|js|xml|pdf|ico|webp)$", path):
        return False
    return True


def transliterate_competitor_token(token: str) -> str:
    translit_map = {
        "а": "a",
        "б": "b",
        "в": "v",
        "г": "g",
        "д": "d",
        "е": "e",
        "ё": "e",
        "ж": "zh",
        "з": "z",
        "и": "i",
        "й": "y",
        "к": "k",
        "л": "l",
        "м": "m",
        "н": "n",
        "о": "o",
        "п": "p",
        "р": "r",
        "с": "s",
        "т": "t",
        "у": "u",
        "ф": "f",
        "х": "h",
        "ц": "ts",
        "ч": "ch",
        "ш": "sh",
        "щ": "sch",
        "ъ": "",
        "ы": "y",
        "ь": "",
        "э": "e",
        "ю": "yu",
        "я": "ya",
    }
    return "".join(translit_map.get(char, char) for char in normalize_competitor_text(token))


def build_competitor_theme_profile(theme: str) -> dict:
    raw_tokens = [
        token
        for token in re.findall(r"[0-9a-zа-яё]+", normalize_competitor_text(theme))
        if len(token) > 2 and token not in {"для", "под", "the", "and"}
    ]
    match_terms: list[str] = []
    seen_terms = set()

    for token in raw_tokens:
        candidates = [token]
        transliterated = transliterate_competitor_token(token)
        if transliterated and transliterated != token:
            candidates.append(transliterated)
        if len(token) > 4 and token[-1] in {"ы", "и", "а", "я", "у", "ю", "е"}:
            truncated = token[:-1]
            if len(truncated) >= 4:
                candidates.append(truncated)
                transliterated_truncated = transliterate_competitor_token(truncated)
                if transliterated_truncated and transliterated_truncated != truncated:
                    candidates.append(transliterated_truncated)

        for candidate in candidates:
            normalized_candidate = normalize_competitor_text(candidate)
            if len(normalized_candidate) < 4 or normalized_candidate in seen_terms:
                continue
            seen_terms.add(normalized_candidate)
            match_terms.append(normalized_candidate)

    return {
        "theme": clean_inline_text(theme),
        "tokens": raw_tokens,
        "match_terms": match_terms,
    }


def find_competitor_matched_terms(source_text: str, terms: list[str]) -> list[str]:
    normalized_source = normalize_competitor_text(source_text)
    matched_terms: list[str] = []
    for term in terms:
        normalized_term = normalize_competitor_text(term)
        if normalized_term and normalized_term in normalized_source and normalized_term not in matched_terms:
            matched_terms.append(normalized_term)
    return matched_terms


def add_competitor_evidence_line(evidence: list[str], source_name: str, matched_term: str) -> None:
    line = f"{source_name} contains {matched_term}"
    if line not in evidence:
        evidence.append(line)


def collect_competitor_page_evidence(page: dict, theme_profile: dict) -> list[str]:
    evidence: list[str] = []
    sources = [
        ("title", page.get("title", "")),
        ("heading", " ".join(page.get("headings", []))),
        ("page text", page.get("text", "")),
        ("url", page.get("url", "")),
    ]

    for source_name, source_value in sources:
        matched_terms = find_competitor_matched_terms(source_value, theme_profile["match_terms"])
        for matched_term in matched_terms[:2]:
            add_competitor_evidence_line(evidence, source_name, matched_term)
        if len(evidence) >= 4:
            break

    return evidence[:4]


def score_competitor_link(link_item: dict, theme_profile: dict, base_url: str) -> int:
    normalized_url = normalize_competitor_page_url(link_item.get("url", ""), base_url)
    if normalized_url is None or not is_competitor_crawl_candidate_url(normalized_url, base_url):
        return -1

    path = urllib.parse.urlsplit(normalized_url).path.lower()
    combined_text = normalize_competitor_text(f"{link_item.get('text', '')} {normalized_url}")
    score = 0

    if any(term in combined_text for term in theme_profile["match_terms"]):
        score += 12
    if any(hint in path for hint in COMPETITOR_PATH_POSITIVE_HINTS):
        score += 3
    if any(hint in combined_text for hint in COMPETITOR_POSITIONING_HINTS):
        score += 1
    if path in {"", "/"}:
        score -= 2

    return score


def extract_competitor_price(text: str) -> Optional[str]:
    if not isinstance(text, str):
        return None
    match = re.search(r"\b\d[\d\s.,]{0,18}\s*₽|\b\d[\d\s.,]{0,18}\s*руб\.?", text, flags=re.IGNORECASE)
    if match is None:
        return None
    return clean_inline_text(match.group(0))


def extract_competitor_positioning_facts(page: dict) -> list[dict]:
    facts: list[dict] = []
    seen_texts = set()
    title = clean_inline_text(page.get("title"))
    if title:
        facts.append({"type": "homepage_title", "source_url": page["url"], "text": title})
        seen_texts.add(normalize_competitor_text(title))

    for heading in page.get("headings", [])[:3]:
        normalized_heading = normalize_competitor_text(heading)
        if not normalized_heading:
            continue
        if any(hint in normalized_heading for hint in COMPETITOR_POSITIONING_HINTS):
            if normalized_heading in seen_texts:
                continue
            facts.append({"type": "homepage_heading", "source_url": page["url"], "text": clean_inline_text(heading)})
            seen_texts.add(normalized_heading)

    raw_sentences = re.split(r"(?<=[.!?])\s+", clean_inline_text(page.get("text", "")))
    for sentence in raw_sentences[:12]:
        normalized_sentence = normalize_competitor_text(sentence)
        if len(normalized_sentence) < 20 or len(normalized_sentence) > 180:
            continue
        if normalized_sentence in seen_texts:
            continue
        if any(hint in normalized_sentence for hint in COMPETITOR_POSITIONING_HINTS):
            facts.append({"type": "homepage_text", "source_url": page["url"], "text": clean_inline_text(sentence)})
            seen_texts.add(normalized_sentence)
        if len(facts) >= 3:
            break

    return facts[:3]


def build_competitor_match_from_page(page: dict, theme_profile: dict) -> Optional[dict]:
    evidence = collect_competitor_page_evidence(page, theme_profile)
    if not evidence:
        return None

    title = clean_inline_text(page.get("title") or " ".join(page.get("headings", [])) or page.get("url", ""))
    if not title:
        return None

    return {
        "page_url": page["url"],
        "title": title,
        "headings": [clean_inline_text(item) for item in page.get("headings", []) if clean_inline_text(item)][:2],
        "price": extract_competitor_price(page.get("text", "")),
        "evidence": evidence,
    }


def build_competitor_confirmed_facts(positioning_facts: list[dict], matched_pages: list[dict]) -> list[dict]:
    facts = list(positioning_facts)
    for matched_page in matched_pages:
        facts.append(
            {
                "type": "theme_match",
                "source_url": matched_page["page_url"],
                "title": matched_page["title"],
                "price": matched_page.get("price"),
                "evidence": matched_page.get("evidence", []),
            }
        )
    return facts


def analyze_competitor_site(theme: str, competitor_url: str) -> dict:
    base_url = normalize_competitor_base_url(competitor_url)
    if base_url is None:
        return {
            "competitor_url": clean_inline_text(competitor_url),
            "status": "error",
            "message": "competitor URL must belong to the allowed competitor list",
            "confirmed_facts": [],
            "matched_pages": [],
            "warnings": [],
        }

    homepage_result = fetch_site_url(base_url)
    if not homepage_result["ok"]:
        return {
            "competitor_url": base_url,
            "status": "fetch_error",
            "message": "failed to fetch competitor homepage",
            "confirmed_facts": [],
            "matched_pages": [],
            "warnings": [homepage_result["payload"]],
        }

    theme_profile = build_competitor_theme_profile(theme)
    homepage_page = parse_artfarfor_page(base_url, homepage_result["text"])
    queue = [(base_url, 0)]
    queued_urls = {base_url}
    visited_urls = set()
    matched_pages: list[dict] = []
    seen_match_urls = set()
    warnings: list[dict] = []

    while queue and len(visited_urls) < MAX_COMPETITOR_ANALYSIS_PAGES:
        current_url, depth = queue.pop(0)
        queued_urls.discard(current_url)
        if current_url in visited_urls:
            continue

        page_result = homepage_result if current_url == base_url else fetch_site_url(current_url)
        visited_urls.add(current_url)
        if not page_result["ok"]:
            warnings.append(
                {
                    "source_url": current_url,
                    "message": "failed to fetch competitor page",
                    "raw": page_result["payload"],
                }
            )
            continue

        page = parse_artfarfor_page(current_url, page_result["text"])
        maybe_match = build_competitor_match_from_page(page, theme_profile)
        if maybe_match is not None and maybe_match["page_url"] not in seen_match_urls:
            matched_pages.append(maybe_match)
            seen_match_urls.add(maybe_match["page_url"])

        if depth >= MAX_COMPETITOR_ANALYSIS_DEPTH:
            continue

        ranked_links = []
        for link_item in page.get("links", []):
            normalized_url = normalize_competitor_page_url(link_item.get("url", ""), current_url)
            if normalized_url is None or normalized_url in visited_urls or normalized_url in queued_urls:
                continue
            if not is_competitor_crawl_candidate_url(normalized_url, base_url):
                continue
            score = score_competitor_link({"url": normalized_url, "text": link_item.get("text", "")}, theme_profile, base_url)
            if score < 0:
                continue
            ranked_links.append((score, normalized_url))

        ranked_links.sort(key=lambda item: item[0], reverse=True)
        for score, next_url in ranked_links[:MAX_COMPETITOR_LINKS_PER_PAGE]:
            if score <= 0 and depth > 0:
                continue
            queue.append((next_url, depth + 1))
            queued_urls.add(next_url)

    positioning_facts = extract_competitor_positioning_facts(homepage_page)
    return {
        "competitor_url": base_url,
        "status": "success",
        "theme_status": "matched" if matched_pages else "no_match",
        "homepage_title": homepage_page.get("title"),
        "confirmed_facts": build_competitor_confirmed_facts(positioning_facts, matched_pages[:5]),
        "matched_pages": matched_pages[:5],
        "warnings": warnings[:6],
    }


def build_competitor_analysis_preview_payload(theme: str, competitors: list[str]) -> dict:
    normalized_theme = normalize_non_empty_string(theme)
    if normalized_theme is None:
        return {"ok": False, "status": 400, "payload": {"status": "error", "message": "theme must be a non-empty string"}}

    normalized_competitors: list[str] = []
    seen_competitors = set()
    for competitor_url in competitors:
        normalized_url = normalize_competitor_base_url(competitor_url)
        if normalized_url is None:
            return {
                "ok": False,
                "status": 400,
                "payload": {
                    "status": "error",
                    "message": "each competitor must be one of the allowed public competitor sites",
                },
            }
        if normalized_url in seen_competitors:
            continue
        seen_competitors.add(normalized_url)
        normalized_competitors.append(normalized_url)

    competitors_payload = [analyze_competitor_site(normalized_theme, competitor_url) for competitor_url in normalized_competitors]
    return {
        "ok": True,
        "status": 200,
        "payload": {
            "status": "success",
            "theme": normalized_theme,
            "summary": {
                "competitors_requested": len(normalized_competitors),
                "competitors_reached": sum(1 for item in competitors_payload if item.get("status") == "success"),
                "theme_matched_competitors": sum(1 for item in competitors_payload if item.get("theme_status") == "matched"),
            },
            "competitors": competitors_payload,
        },
    }


def build_theme_profile(theme: str) -> dict:
    normalized = normalize_match_text(theme)
    tokens = tokenize_match_text(theme)
    match_type = "category"
    if re.search(r"[a-z]", normalized):
        match_type = "brand"
    elif any(token.startswith(prefix) for token in tokens for prefix in ("подар", "жен", "мам", "юбил")):
        match_type = "gift"
    elif any(token.startswith(prefix) for token in tokens for prefix in ("немец", "герман", "англ", "британ", "итал", "франц", "испан")):
        match_type = "origin"

    origin_terms = []
    if match_type == "origin":
        if any(token.startswith(prefix) for token in tokens for prefix in ("немец", "герман")):
            origin_terms.extend(["германия", "немец"])
        if any(token.startswith(prefix) for token in tokens for prefix in ("англ", "британ")):
            origin_terms.extend(["англия", "британ"])
        if any(token.startswith(prefix) for token in tokens for prefix in ("итал",)):
            origin_terms.extend(["италия", "итали"])
        if any(token.startswith(prefix) for token in tokens for prefix in ("франц",)):
            origin_terms.extend(["франция", "франц"])
        if any(token.startswith(prefix) for token in tokens for prefix in ("испан",)):
            origin_terms.extend(["испания", "испан"])

    category_terms = []
    for token in tokens:
        if "фарфор" in token:
            category_terms.append("фарфор")
        elif token.startswith("стату"):
            category_terms.append("статуэт")
        elif token.startswith("тарел"):
            category_terms.append("тарел")
        elif token.startswith("ваз"):
            category_terms.append("ваз")
        elif token.startswith("подар"):
            category_terms.append("подар")
    if not category_terms:
        category_terms = tokens[:]

    gift_terms = [token for token in tokens if token.startswith("подар")]
    audience_terms = []
    for token in tokens:
        if token.startswith("жен"):
            audience_terms.extend(["женщ", "для нее", "для неё"])
        elif token.startswith("мам"):
            audience_terms.append("мам")
        elif token.startswith("юбил"):
            audience_terms.append("юбил")

    return {
        "theme": clean_inline_text(theme),
        "normalized": normalized,
        "tokens": tokens,
        "match_type": match_type,
        "origin_terms": list(dict.fromkeys(origin_terms)),
        "category_terms": list(dict.fromkeys(category_terms)),
        "gift_terms": list(dict.fromkeys(gift_terms)),
        "audience_terms": list(dict.fromkeys(audience_terms)),
    }


def find_matched_terms(source_text: str, terms: list) -> list:
    matched = []
    normalized_source = normalize_match_text(source_text)
    for term in terms:
        normalized_term = normalize_match_text(term)
        if normalized_term and normalized_term in normalized_source and normalized_term not in matched:
            matched.append(normalized_term)
    return matched


def add_evidence_line(evidence: list, source_name: str, message: str) -> None:
    line = f"{source_name} contains {message}"
    if line not in evidence:
        evidence.append(line)


def collect_page_match_evidence(page: dict, theme_profile: dict) -> list:
    normalized_theme = theme_profile["normalized"]
    sources = [
        ("title", page.get("title", "")),
        ("heading", " ".join(page.get("headings", []))),
        ("page text", page.get("text", "")),
    ]
    evidence = []

    if theme_profile["match_type"] == "brand":
        for source_name, source_value in sources[:2]:
            if normalized_theme and normalized_theme in normalize_match_text(source_value):
                add_evidence_line(evidence, source_name, theme_profile["theme"])
        return evidence

    if theme_profile["match_type"] == "gift":
        for source_name, source_value in sources:
            normalized_source = normalize_match_text(source_value)
            if normalized_theme and normalized_theme in normalized_source:
                add_evidence_line(evidence, source_name, theme_profile["theme"])
                continue
            matched_gift_terms = find_matched_terms(normalized_source, theme_profile["gift_terms"])
            matched_audience_terms = find_matched_terms(normalized_source, theme_profile["audience_terms"])
            if matched_gift_terms and matched_audience_terms:
                add_evidence_line(evidence, source_name, matched_gift_terms[0])
                add_evidence_line(evidence, source_name, matched_audience_terms[0])
        return evidence

    if theme_profile["match_type"] == "origin":
        for source_name, source_value in sources:
            normalized_source = normalize_match_text(source_value)
            if normalized_theme and normalized_theme in normalized_source:
                add_evidence_line(evidence, source_name, theme_profile["theme"])
                continue
            matched_origin_terms = find_matched_terms(normalized_source, theme_profile["origin_terms"])
            matched_category_terms = find_matched_terms(normalized_source, theme_profile["category_terms"])
            if matched_origin_terms and matched_category_terms:
                add_evidence_line(evidence, source_name, matched_origin_terms[0])
                add_evidence_line(evidence, source_name, matched_category_terms[0])
        return evidence

    combined_text = normalize_match_text(" ".join(source for _, source in sources))
    if normalized_theme and normalized_theme in combined_text:
        add_evidence_line(evidence, "page text", theme_profile["theme"])
        return evidence

    matched_category_terms = find_matched_terms(combined_text, theme_profile["category_terms"])
    if matched_category_terms and len(matched_category_terms) >= min(2, len(theme_profile["category_terms"])):
        for matched_term in matched_category_terms[:3]:
            add_evidence_line(evidence, "page text", matched_term)

    return evidence


def is_decorative_site_image(image_item: dict) -> bool:
    image_text = normalize_match_text(
        " ".join(
            [
                image_item.get("url", ""),
                image_item.get("alt", ""),
                image_item.get("class", ""),
            ]
        )
    )
    return any(hint in image_text for hint in DECORATIVE_IMAGE_HINTS)


def select_best_site_image(page: dict, theme_profile: dict) -> Optional[dict]:
    candidates = []
    seen_urls = set()

    for meta_image_url in page.get("meta_image_urls", []):
        normalized_url = normalize_site_image_url(meta_image_url, page["url"])
        if normalized_url and normalized_url not in seen_urls:
            candidates.append({"url": normalized_url, "alt": "", "class": "", "source_type": "meta"})
            seen_urls.add(normalized_url)

    for image_item in page.get("images", []):
        normalized_url = normalize_site_image_url(image_item.get("url", ""), page["url"])
        if normalized_url and normalized_url not in seen_urls:
            candidate = dict(image_item)
            candidate["url"] = normalized_url
            candidate["source_type"] = "img"
            candidates.append(candidate)
            seen_urls.add(normalized_url)

    scored_candidates = []
    for candidate in candidates:
        if is_decorative_site_image(candidate):
            continue

        candidate_text = normalize_match_text(
            " ".join(
                [
                    candidate.get("alt", ""),
                    page.get("title", ""),
                    " ".join(page.get("headings", [])),
                ]
            )
        )
        score = 0
        if candidate.get("source_type") == "meta":
            score += 5
        if theme_profile["normalized"] and theme_profile["normalized"] in candidate_text:
            score += 8
        for token in theme_profile["tokens"]:
            if token in candidate_text:
                score += 2
        if normalize_match_text(candidate.get("alt", "")):
            score += 1
        scored_candidates.append((score, candidate))

    if not scored_candidates:
        return None

    scored_candidates.sort(key=lambda item: item[0], reverse=True)
    return scored_candidates[0][1]


def score_site_link(link_item: dict, theme_profile: dict) -> int:
    url = normalize_artfarfor_url(link_item.get("url", ""))
    if url is None or not is_crawl_candidate_url(url):
        return -1

    combined = normalize_match_text(f"{link_item.get('text', '')} {url}")
    score = 0
    if theme_profile["normalized"] and theme_profile["normalized"] in combined:
        score += 10
    for token in theme_profile["tokens"]:
        if token in combined:
            score += 2
    for token in theme_profile["origin_terms"] + theme_profile["gift_terms"] + theme_profile["audience_terms"]:
        if token in combined:
            score += 2
    path = urllib.parse.urlsplit(url).path.lower()
    if path.startswith("/collection"):
        score += 1
    if path.startswith("/product"):
        score += 2
    return score


def build_site_match_from_page(page: dict, theme_profile: dict) -> Optional[dict]:
    if urllib.parse.urlsplit(page["url"]).path in {"", "/"}:
        return None

    evidence = collect_page_match_evidence(page, theme_profile)
    if not evidence:
        return None

    image_candidate = select_best_site_image(page, theme_profile)
    if image_candidate is None:
        return None

    title = clean_inline_text(image_candidate.get("alt") or page.get("title") or " ".join(page.get("headings", [])))
    if not title:
        return None

    return {
        "page_url": page["url"],
        "image_url": image_candidate["url"],
        "title": title,
        "evidence": evidence[:4],
        "match_type": theme_profile["match_type"],
    }


def find_site_images_for_theme_internal(theme: str, limit: int = DEFAULT_SITE_IMAGE_LIMIT) -> dict:
    theme_profile = build_theme_profile(theme)
    homepage_result = fetch_site_url(ARTFARFOR_BASE_URL)
    if not homepage_result["ok"]:
        return {"ok": False, "status": homepage_result["status_code"], "payload": {"status": "error", "message": "failed to fetch artfarfor.com homepage", "raw": homepage_result["payload"]}}

    queue = [(ARTFARFOR_BASE_URL, 0)]
    queued_urls = {ARTFARFOR_BASE_URL}
    visited_urls = set()
    matches = []
    seen_match_keys = set()

    while queue and len(visited_urls) < MAX_SITE_DISCOVERY_PAGES and len(matches) < limit:
        current_url, depth = queue.pop(0)
        queued_urls.discard(current_url)
        if current_url in visited_urls:
            continue

        page_result = homepage_result if current_url == ARTFARFOR_BASE_URL else fetch_site_url(current_url)
        if not page_result["ok"]:
            visited_urls.add(current_url)
            continue

        page = parse_artfarfor_page(current_url, page_result["text"])
        visited_urls.add(current_url)

        maybe_match = build_site_match_from_page(page, theme_profile)
        if maybe_match is not None:
            match_key = (maybe_match["page_url"], maybe_match["image_url"])
            if match_key not in seen_match_keys:
                matches.append(maybe_match)
                seen_match_keys.add(match_key)
                if len(matches) >= limit:
                    break

        if depth >= 2:
            continue

        ranked_links = []
        for link_item in page.get("links", []):
            normalized_url = normalize_artfarfor_url(link_item.get("url", ""), page["url"])
            if normalized_url is None or normalized_url in visited_urls or normalized_url in queued_urls:
                continue
            score = score_site_link({"url": normalized_url, "text": link_item.get("text", "")}, theme_profile)
            if score < 0:
                continue
            ranked_links.append((score, normalized_url))

        ranked_links.sort(key=lambda item: item[0], reverse=True)
        for score, next_url in ranked_links[:MAX_SITE_LINKS_PER_PAGE]:
            if score <= 0 and depth > 0:
                continue
            queue.append((next_url, depth + 1))
            queued_urls.add(next_url)

    return {"ok": True, "status": 200, "payload": {"status": "success" if matches else "no_match", "theme": theme_profile["theme"], "matches": matches[:limit]}}


def download_site_image_bytes(image_url: str) -> dict:
    normalized_url = normalize_site_image_url(image_url)
    if normalized_url is None:
        return {"ok": False, "status": 400, "payload": {"status": "error", "message": "image_url must belong to artfarfor.com or static.insales-cdn.com"}}

    result = fetch_site_url(normalized_url, binary=True)
    if not result["ok"]:
        return {"ok": False, "status": result["status_code"], "payload": {"status": "error", "message": "failed to download image from allowed site image host", "raw": result["payload"]}}

    content_type = result.get("content_type", "")
    if content_type and not content_type.lower().startswith("image/"):
        return {"ok": False, "status": 400, "payload": {"status": "error", "message": "downloaded URL is not an image", "raw": {"content_type": content_type}}}

    return {
        "ok": True,
        "status": 200,
        "payload": {
            "image_bytes": result["body"],
            "content_type": content_type,
            "filename": os.path.basename(urllib.parse.urlsplit(normalized_url).path) or "artfarfor-site-image.jpg",
            "image_url": normalized_url,
        },
    }


def upload_ad_image_via_direct(target: str, name: str, image_data_base64: str) -> dict:
    client = YandexDirectClient.for_target(target)
    try:
        result = client.add_ad_image(name=name, image_data_base64=image_data_base64)
    except YandexDirectClientError as e:
        return {"ok": False, "status": 502, "payload": {"status": "error", "message": str(e), "target": target}}

    parsed = parse_ad_image_add_result(result)
    if not parsed["ok"]:
        payload = parsed["payload"]
        payload["target"] = target
        return {"ok": False, "status": parsed["status"], "payload": payload}

    return {
        "ok": True,
        "status": 200,
        "payload": {
            "result": result,
            "ad_image_hash": parsed["payload"]["ad_image_hash"],
            "warnings": parsed["payload"]["warnings"],
        },
    }


def add_yandex_uploaded_image_to_state(state: dict, ad_image_hash: str) -> dict:
    if not isinstance(state.get("media_library"), list):
        state["media_library"] = []
    state["last_uploaded_image_hash"] = ad_image_hash
    state["media_library"].append(
        {
            "type": "image",
            "source": "yandex_direct_upload",
            "ad_image_hash": ad_image_hash,
        }
    )
    return state


def normalize_ad_image_hashes(raw_hashes, fallback_hash=None) -> List[str]:
    normalized_hashes: List[str] = []
    seen_hashes = set()

    if isinstance(raw_hashes, list):
        for item in raw_hashes:
            normalized = normalize_non_empty_string(item)
            if normalized is None or normalized in seen_hashes:
                continue
            normalized_hashes.append(normalized)
            seen_hashes.add(normalized)

    fallback = normalize_non_empty_string(fallback_hash)
    if fallback is not None and fallback not in seen_hashes:
        normalized_hashes.insert(0, fallback)

    return normalized_hashes


def normalize_draft_ad_images(draft_ad: dict) -> dict:
    if not isinstance(draft_ad, dict):
        return draft_ad

    normalized_hashes = normalize_ad_image_hashes(
        draft_ad.get("ad_image_hashes"),
        fallback_hash=draft_ad.get("ad_image_hash"),
    )
    draft_ad["ad_image_hashes"] = normalized_hashes
    draft_ad["ad_image_hash"] = normalized_hashes[0] if normalized_hashes else None
    return draft_ad


def normalize_draft_campaign_images(draft_campaign) -> None:
    if not isinstance(draft_campaign, dict):
        return

    raw_campaign_ads = draft_campaign.get("ads")
    if isinstance(raw_campaign_ads, list):
        for draft_ad in raw_campaign_ads:
            normalize_draft_ad_images(draft_ad)

    raw_ad_groups = draft_campaign.get("ad_groups")
    if not isinstance(raw_ad_groups, list):
        return

    for ad_group in raw_ad_groups:
        if not isinstance(ad_group, dict):
            continue
        raw_group_ads = ad_group.get("ads")
        if not isinstance(raw_group_ads, list):
            continue
        for draft_ad in raw_group_ads:
            normalize_draft_ad_images(draft_ad)


def enumerate_draft_ad_references(draft_campaign: dict) -> list[dict]:
    references: list[dict] = []
    if not isinstance(draft_campaign, dict):
        return references

    raw_campaign_ads = draft_campaign.get("ads")
    if isinstance(raw_campaign_ads, list):
        for ad_index, draft_ad in enumerate(raw_campaign_ads):
            if not isinstance(draft_ad, dict):
                continue
            references.append(
                {
                    "scope": "campaign",
                    "ad_index": ad_index,
                }
            )

    raw_ad_groups = draft_campaign.get("ad_groups")
    if not isinstance(raw_ad_groups, list):
        return references

    for ad_group_index, ad_group in enumerate(raw_ad_groups):
        if not isinstance(ad_group, dict):
            continue
        raw_group_ads = ad_group.get("ads")
        if not isinstance(raw_group_ads, list):
            continue
        for ad_index, draft_ad in enumerate(raw_group_ads):
            if not isinstance(draft_ad, dict):
                continue
            references.append(
                {
                    "scope": "ad_group",
                    "ad_group_index": ad_group_index,
                    "ad_index": ad_index,
                }
            )

    return references


def append_draft_media_item(
    state: dict,
    link_ref: dict,
    ad_image_hash: str,
    creative_spec: Optional[dict] = None,
) -> None:
    if not isinstance(state.get("draft_media"), list):
        state["draft_media"] = []

    draft_media_item = {
        "type": "image",
        "scope": link_ref["scope"],
        "ad_image_hash": ad_image_hash,
        **({} if "ad_group_index" not in link_ref else {"ad_group_index": link_ref["ad_group_index"]}),
        "ad_index": link_ref["ad_index"],
    }
    if isinstance(creative_spec, dict):
        draft_media_item["creative_spec"] = deep_copy_json(creative_spec)

    matched_item = None
    for existing_item in state["draft_media"]:
        if not isinstance(existing_item, dict):
            continue
        if existing_item.get("scope") != link_ref["scope"]:
            continue
        if existing_item.get("ad_index") != link_ref["ad_index"]:
            continue
        if existing_item.get("ad_image_hash") != ad_image_hash:
            continue
        if link_ref["scope"] == "ad_group" and existing_item.get("ad_group_index") != link_ref.get("ad_group_index"):
            continue
        if link_ref["scope"] == "campaign" and existing_item.get("ad_group_index") is not None:
            continue
        matched_item = existing_item
        break

    if matched_item is None:
        state["draft_media"].append(draft_media_item)
    elif "creative_spec" in draft_media_item:
        matched_item["creative_spec"] = deep_copy_json(draft_media_item["creative_spec"])


def link_image_hashes_to_draft_state(
    state: dict,
    scope: str,
    ad_index: int,
    ad_group_index: Optional[int],
    ad_image_hashes: List[str],
) -> dict:
    normalized_hashes = normalize_ad_image_hashes(ad_image_hashes)
    if not normalized_hashes:
        return {"ok": False, "status": 400, "payload": {"status": "error", "message": "ad_image_hashes must contain at least one non-empty string"}}

    draft_campaign = state.get("draft_campaign")
    if not isinstance(draft_campaign, dict):
        return {"ok": False, "status": 409, "payload": {"status": "error", "message": "draft_campaign is not initialized"}}

    normalize_draft_campaign_images(draft_campaign)

    draft_ad, link_ref = resolve_draft_ad_reference(
        draft_campaign=draft_campaign,
        scope=scope,
        ad_index=ad_index,
        ad_group_index=ad_group_index,
    )
    if draft_ad is None or link_ref is None:
        return {"ok": False, "status": 400, "payload": {"status": "error", "message": "draft ad not found for provided scope/indexes"}}

    normalize_draft_ad_images(draft_ad)
    existing_hashes = list(draft_ad.get("ad_image_hashes") or [])
    for ad_image_hash in normalized_hashes:
        if ad_image_hash not in existing_hashes:
            existing_hashes.append(ad_image_hash)

    draft_ad["ad_image_hashes"] = existing_hashes
    draft_ad["ad_image_hash"] = existing_hashes[0] if existing_hashes else None

    creative_spec = state.get("creative_spec")
    if isinstance(creative_spec, dict):
        draft_ad["creative_spec"] = deep_copy_json(creative_spec)

    for ad_image_hash in normalized_hashes:
        append_draft_media_item(state, link_ref, ad_image_hash, creative_spec if isinstance(creative_spec, dict) else None)

    state["draft_campaign"] = draft_campaign
    return {
        "ok": True,
        "status": 200,
        "payload": {
            "state": state,
            "draft_fragment": draft_ad,
            "draft_campaign": state["draft_campaign"],
            "draft_media": state["draft_media"],
        },
    }


def link_image_hash_to_draft_state(state: dict, scope: str, ad_index: int, ad_group_index: Optional[int], ad_image_hash: str) -> dict:
    return link_image_hashes_to_draft_state(
        state=state,
        scope=scope,
        ad_index=ad_index,
        ad_group_index=ad_group_index,
        ad_image_hashes=[ad_image_hash],
    )


normalize_draft_campaign_images(RUNTIME_STATE.get("draft_campaign"))


def normalize_draft_final_url(value) -> Optional[str]:
    normalized = normalize_non_empty_string(value)
    if normalized is None:
        return None
    if not normalized.startswith("https://artfarfor.com"):
        return None
    return normalized


def resolve_draft_ad_reference(
    draft_campaign: dict,
    scope: str,
    ad_index: int,
    ad_group_index: Optional[int] = None,
):
    if scope == "campaign":
        ads = draft_campaign.get("ads")
        if not isinstance(ads, list) or ad_index >= len(ads):
            return None, None
        ad = ads[ad_index]
        if not isinstance(ad, dict):
            return None, None
        return ad, {"scope": "campaign", "ad_index": ad_index}

    if scope == "ad_group":
        if ad_group_index is None:
            return None, None
        ad_groups = draft_campaign.get("ad_groups")
        if not isinstance(ad_groups, list) or ad_group_index >= len(ad_groups):
            return None, None
        ad_group = ad_groups[ad_group_index]
        if not isinstance(ad_group, dict):
            return None, None
        ads = ad_group.get("ads")
        if not isinstance(ads, list) or ad_index >= len(ads):
            return None, None
        ad = ads[ad_index]
        if not isinstance(ad, dict):
            return None, None
        return ad, {"scope": "ad_group", "ad_group_index": ad_group_index, "ad_index": ad_index}

    return None, None


def resolve_draft_ad_group_reference(draft_campaign: dict, ad_group_index: int):
    ad_groups = draft_campaign.get("ad_groups")
    if not isinstance(ad_groups, list) or ad_group_index >= len(ad_groups):
        return None

    ad_group = ad_groups[ad_group_index]
    if not isinstance(ad_group, dict):
        return None

    return ad_group


def build_campaign_payload_from_draft_state(draft_campaign: dict):
    payload = {
        "target": "production",
        "campaign_name": draft_campaign.get("campaign_name"),
        "site_url": draft_campaign.get("site_url"),
        "region": draft_campaign.get("region"),
        "language": draft_campaign.get("language"),
        "placement_type": draft_campaign.get("placement_type"),
        "goal_type": draft_campaign.get("goal_type"),
        "strategy_type": draft_campaign.get("strategy_type"),
        "metrica_goal_id": draft_campaign.get("metrica_goal_id"),
        "weekly_budget_rub": draft_campaign.get("weekly_budget_rub"),
        "target_cpa_rub": draft_campaign.get("target_cpa_rub"),
        "utm_tracking": draft_campaign.get("utm_tracking"),
        "ad_groups": draft_campaign.get("ad_groups", []),
        "ads": draft_campaign.get("ads", []),
        "tracking_params": draft_campaign.get("tracking_params"),
        "negative_keywords": draft_campaign.get("negative_keywords", []),
        "sitelinks": draft_campaign.get("sitelinks", []),
        "autotargeting_settings": draft_campaign.get("autotargeting_settings"),
        "campaign_type": draft_campaign.get("campaign_type"),
        "metrica_counter_id": draft_campaign.get("metrica_counter_id"),
        "assumptions": draft_campaign.get("assumptions", []),
    }

    missing_fields = []
    for field_name in (
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
    ):
        if payload.get(field_name) in ("", None):
            missing_fields.append(field_name)

    return payload, missing_fields


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


def parse_ad_image_add_result(result: dict) -> dict:
    add_results = result.get("result", {}).get("AddResults", [])
    if not add_results:
        return {
            "ok": False,
            "status": 502,
            "payload": {
                "status": "error",
                "message": "empty AddResults from Yandex Direct for ad_image",
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
                "message": "Yandex Direct rejected ad_image create",
                "errors": first["Errors"],
                "raw": result,
            },
        }

    ad_image_hash = first.get("AdImageHash")
    if not isinstance(ad_image_hash, str) or not ad_image_hash.strip():
        return {
            "ok": False,
            "status": 502,
            "payload": {
                "status": "error",
                "message": "missing AdImageHash in Yandex Direct add response for ad_image",
                "raw": result,
            },
        }

    return {
        "ok": True,
        "status": 200,
        "payload": {
            "status": "success",
            "ad_image_hash": ad_image_hash.strip(),
            "warnings": first.get("Warnings", []),
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


def parse_action_results(result: dict, result_key: str, entity_name: str, action_name: str) -> dict:
    action_results = result.get("result", {}).get(result_key, [])
    if not action_results:
        return {
            "ok": False,
            "status": 502,
            "payload": {
                "status": "error",
                "message": f"empty {result_key} from Yandex Direct for {entity_name} {action_name}",
                "raw": result,
            },
        }

    items = []
    has_errors = False

    for item in action_results:
        payload_item = {
            "id": str(item.get("Id")),
            "warnings": item.get("Warnings", []),
            "errors": item.get("Errors", []),
        }
        if payload_item["errors"]:
            has_errors = True
        items.append(payload_item)

    if has_errors:
        return {
            "ok": False,
            "status": 400,
            "payload": {
                "status": "error",
                "message": f"Yandex Direct rejected {entity_name} {action_name}",
                "results": items,
                "raw": result,
            },
        }

    return {
        "ok": True,
        "status": 200,
        "payload": {
            "status": "success",
            "results": items,
        },
    }


def collect_action_error_codes(results: list) -> list[int]:
    codes: list[int] = []
    seen_codes = set()
    if not isinstance(results, list):
        return codes

    for item in results:
        if not isinstance(item, dict):
            continue
        raw_errors = item.get("errors")
        if not isinstance(raw_errors, list):
            continue
        for raw_error in raw_errors:
            if not isinstance(raw_error, dict):
                continue
            code = raw_error.get("Code")
            if not isinstance(code, int) or code in seen_codes:
                continue
            seen_codes.add(code)
            codes.append(code)

    return codes


def collect_campaign_entities_for_replace(client: YandexDirectClient, campaign_id: int) -> dict:
    campaign_result = client.get_campaign_details(campaign_id)
    campaigns = campaign_result.get("result", {}).get("Campaigns", [])
    if not campaigns:
        return {
            "ok": False,
            "status": 404,
            "payload": {
                "status": "error",
                "message": "campaign not found",
                "target": "production",
                "campaign_id": str(campaign_id),
                "raw": campaign_result,
            },
        }

    ad_groups_result = client.list_ad_groups(campaign_id)
    raw_ad_groups = ad_groups_result.get("result", {}).get("AdGroups", [])
    ad_group_ids: list[int] = []
    ad_ids: list[int] = []
    warnings: list[str] = []
    region_ids_for_create = None

    for raw_ad_group in raw_ad_groups:
        if not isinstance(raw_ad_group, dict):
            continue

        ad_group_id = normalize_ad_group_id(raw_ad_group.get("Id"))
        if ad_group_id is None:
            continue

        ad_group_ids.append(ad_group_id)

        if region_ids_for_create is None:
            normalized_region_ids = normalize_region_ids(raw_ad_group.get("RegionIds"))
            if normalized_region_ids:
                region_ids_for_create = normalized_region_ids

        try:
            ads_result = client.list_ads(ad_group_id)
        except YandexDirectClientError as e:
            warnings.append(f"Could not list ads for existing ad_group {ad_group_id}: {str(e)}")
            continue

        raw_ads = ads_result.get("result", {}).get("Ads", [])
        for raw_ad in raw_ads:
            if not isinstance(raw_ad, dict):
                continue
            ad_id = normalize_ad_id(raw_ad.get("Id"))
            if ad_id is not None:
                ad_ids.append(ad_id)

    if region_ids_for_create is None:
        region_ids_for_create = list(DEFAULT_PRODUCTION_REGION_IDS)
        warnings.append(
            "Assumption: used default region_ids [225] because current campaign had no reusable ad group region_ids."
        )

    return {
        "ok": True,
        "status": 200,
        "payload": {
            "campaign": campaigns[0],
            "ad_group_ids": ad_group_ids,
            "ad_ids": ad_ids,
            "region_ids": region_ids_for_create,
            "warnings": warnings,
        },
    }


def build_cleanup_action_payload(status: str, **extra) -> dict:
    payload = {"status": status}
    payload.update(extra)
    return payload


def build_theme_tracking_token(theme: str) -> str:
    if not isinstance(theme, str):
        return "artfarfor"
    raw_tokens = re.findall(r"\w+", theme.lower(), flags=re.UNICODE)
    normalized_tokens = [token for token in raw_tokens if token]
    if not normalized_tokens:
        return "artfarfor"
    return urllib.parse.quote("-".join(normalized_tokens[:4]), safe="")


def infer_campaign_theme(campaign: dict, ad_groups: list) -> Optional[str]:
    campaign_name = normalize_non_empty_string(campaign.get("Name")) if isinstance(campaign, dict) else None
    if campaign_name:
        theme_candidate = campaign_name.split("|", 1)[0].strip()
        if theme_candidate:
            return theme_candidate

    if isinstance(ad_groups, list):
        for ad_group in ad_groups:
            if not isinstance(ad_group, dict):
                continue
            ad_group_name = normalize_non_empty_string(ad_group.get("Name") or ad_group.get("name"))
            if ad_group_name:
                return ad_group_name

    return None


def build_campaign_enrichment_tracking_params(theme: str) -> str:
    return f"{DEFAULT_DRAFT_TRACKING_PARAMS}&utm_theme={build_theme_tracking_token(theme)}"


def build_campaign_enrichment_negative_keywords(theme: str) -> list[str]:
    base_keywords = [
        "оптом",
        "бу",
        "бесплатно",
        "скачать",
        "фото",
        "картинки",
        "обои",
        "авито",
        "ozon",
        "wildberries",
    ]

    lowered_theme = (theme or "").lower()
    if "клоун" in lowered_theme:
        base_keywords.extend(
            [
                "аниматор",
                "цирк",
                "костюм",
                "грим",
                "раскраска",
                "рисунок",
            ]
        )

    deduplicated = []
    seen = set()
    for keyword in base_keywords:
        normalized_keyword = normalize_non_empty_string(keyword)
        if normalized_keyword is None or normalized_keyword in seen:
            continue
        seen.add(normalized_keyword)
        deduplicated.append(normalized_keyword)
    return deduplicated


def normalize_callout_text_for_direct(text: str) -> Optional[str]:
    normalized_text = clean_inline_text(text)
    if not normalized_text:
        return None
    normalized_text = re.sub(r"\s+", " ", normalized_text).strip(" .,-")
    if len(normalized_text) <= 25:
        return normalized_text
    truncated = normalized_text[:25].rstrip()
    if " " in truncated:
        truncated = truncated.rsplit(" ", 1)[0]
    truncated = truncated.strip(" .,-")
    return truncated or None


def build_campaign_enrichment_callouts(theme: str) -> list[str]:
    candidates = [
        normalize_callout_text_for_direct(f"{theme} из фарфора"),
        normalize_callout_text_for_direct("Для подарка"),
        normalize_callout_text_for_direct("Для интерьера"),
        normalize_callout_text_for_direct("Для коллекции"),
        normalize_callout_text_for_direct(theme),
    ]
    callouts: list[str] = []
    seen = set()
    for candidate in candidates:
        if candidate is None or candidate in seen:
            continue
        seen.add(candidate)
        callouts.append(candidate)
        if len(callouts) >= 4:
            break
    return callouts


def normalize_sitelink_title_for_direct(title: str, fallback_theme: str) -> str:
    normalized_title = clean_inline_text(title) or clean_inline_text(fallback_theme) or "ArtFarfor"
    normalized_title = re.sub(r"\s+\|\s+.*$", "", normalized_title)
    normalized_title = re.sub(r"\s+[–-]\s+купить.*$", "", normalized_title, flags=re.IGNORECASE)
    normalized_title = re.sub(r"^Статуэтка\.\s*Фарфор\.\s*", "", normalized_title, flags=re.IGNORECASE)
    normalized_title = re.sub(r"\s+\d[\d\s]*₽\s*$", "", normalized_title)
    normalized_title = re.sub(r"\s+", " ", normalized_title).strip(" .,-")
    if len(normalized_title) <= 30:
        return normalized_title
    for fragment in re.split(r"\.\s*", normalized_title):
        fragment = fragment.strip(" .,-")
        if 4 <= len(fragment) <= 30:
            return fragment
    truncated = normalized_title[:30].rstrip()
    if " " in truncated:
        truncated = truncated.rsplit(" ", 1)[0]
    truncated = truncated.strip(" .,-")
    if truncated:
        return truncated
    fallback_title = clean_inline_text(fallback_theme) or "ArtFarfor"
    return (fallback_title[:30].strip(" .,-") or "ArtFarfor")


def collect_safe_homepage_sitelinks(theme: str, limit: int = 4) -> tuple[list[dict], list[str]]:
    warnings: list[str] = []
    homepage_result = fetch_site_url(ARTFARFOR_BASE_URL)
    if not homepage_result["ok"]:
        warnings.append("Could not fetch artfarfor.com homepage for thematic sitelinks; used safe fallback links.")
        return (
            [
                {"title": "Каталог", "href": "https://artfarfor.com"},
                {"title": "Коллекции", "href": "https://artfarfor.com"},
                {"title": "Подарки", "href": "https://artfarfor.com"},
                {"title": clean_inline_text(theme) or "ArtFarfor", "href": "https://artfarfor.com"},
            ][:limit],
            warnings,
        )

    page = parse_artfarfor_page(ARTFARFOR_BASE_URL, homepage_result["text"])
    theme_profile = build_theme_profile(theme)
    themed_candidates = []
    fallback_candidates = []
    seen_urls = set()

    for link_item in page.get("links", []):
        normalized_url = normalize_artfarfor_url(link_item.get("url", ""), ARTFARFOR_BASE_URL)
        if normalized_url is None or normalized_url in seen_urls:
            continue
        title = normalize_non_empty_string(link_item.get("text"))
        if title is None:
            continue
        seen_urls.add(normalized_url)
        candidate = {
            "title": normalize_sitelink_title_for_direct(title, theme),
            "href": normalized_url,
        }
        score = score_site_link({"url": normalized_url, "text": title}, theme_profile)
        if score > 0:
            themed_candidates.append((score, candidate))
        elif normalized_url == ARTFARFOR_BASE_URL or "/collection/" in normalized_url:
            fallback_candidates.append(candidate)

    themed_candidates.sort(key=lambda item: item[0], reverse=True)
    sitelinks = [candidate for _, candidate in themed_candidates[:limit]]

    for candidate in fallback_candidates:
        if len(sitelinks) >= limit:
            break
        if any(existing["href"] == candidate["href"] for existing in sitelinks):
            continue
        sitelinks.append(candidate)

    fallback_defaults = [
        {"title": "Каталог", "href": "https://artfarfor.com"},
        {"title": "Коллекции", "href": "https://artfarfor.com"},
        {"title": "Подарки", "href": "https://artfarfor.com"},
        {"title": clean_inline_text(theme) or "ArtFarfor", "href": "https://artfarfor.com"},
    ]
    for fallback_item in fallback_defaults:
        if len(sitelinks) >= limit:
            break
        if any(existing["href"] == fallback_item["href"] for existing in sitelinks):
            continue
        sitelinks.append(fallback_item)

    if len(themed_candidates) < min(limit, 4):
        warnings.append("Thematic sitelinks were only partially confirmed from the current site; safe artfarfor.com links were added as fallback.")

    return sitelinks[:limit], warnings


def build_campaign_enrichment_not_confirmed() -> list[str]:
    return [
        "Interests and habits targeting is not confirmed for the current backend flow: official Yandex Direct API docs confirm audience interests for MOBILE_APP_AD_GROUP and user-profile audience targets for CPM_BANNER USER_PROFILE or CPM_VIDEO, not the current UNIFIED campaign enrichment flow.",
        "WordStat integration is not confirmed for the current backend flow: the official Wordstat API exists as a separate API at api.wordstat.yandex.net and requires separate API access setup, while the current backend has no confirmed client, routes, or environment configuration for it.",
        "Competitor analysis automation is not confirmed by the current backend.",
    ]


def extract_group_negative_terms(group_name: str, theme: str) -> list[str]:
    raw_tokens = re.findall(r"\w+", (group_name or "").lower(), flags=re.UNICODE)
    theme_tokens = set(re.findall(r"\w+", (theme or "").lower(), flags=re.UNICODE))
    stopwords = {
        "для",
        "в",
        "из",
        "и",
        "на",
        "с",
        "по",
        "artfarfor",
        "артфарфор",
        "upc",
        "production",
        "group",
    }

    normalized_terms: list[str] = []
    seen_terms = set()
    for token in raw_tokens:
        if token in theme_tokens or token in stopwords or len(token) < 3:
            continue
        if token in seen_terms:
            continue
        seen_terms.add(token)
        normalized_terms.append(token)
    return normalized_terms


def build_campaign_enrichment_group_negative_keywords(
    ad_groups: list[dict],
    theme: str,
) -> list[dict]:
    prepared_groups: list[dict] = []
    all_terms: list[str] = []

    for ad_group in ad_groups:
        if not isinstance(ad_group, dict):
            continue
        group_name = normalize_non_empty_string(ad_group.get("Name") or ad_group.get("name"))
        ad_group_id = normalize_positive_int_id(ad_group.get("Id") or ad_group.get("id"))
        if group_name is None or ad_group_id is None:
            continue
        current_terms = extract_group_negative_terms(group_name, theme)
        prepared_groups.append(
            {
                "ad_group_id": ad_group_id,
                "group_name": group_name,
                "current_terms": current_terms,
            }
        )
        for term in current_terms:
            if term not in all_terms:
                all_terms.append(term)

    result: list[dict] = []
    for prepared_group in prepared_groups:
        current_terms = prepared_group["current_terms"]
        negative_keywords = [term for term in all_terms if term not in current_terms]
        result.append(
            {
                "ad_group_id": str(prepared_group["ad_group_id"]),
                "group_name": prepared_group["group_name"],
                "negative_keywords": negative_keywords,
            }
        )

    return result


CAMPAIGN_ENRICHMENT_KEYWORD_TECHNICAL_TOKENS = {
    "artfarfor",
    "артфарфор",
    "openclaw",
    "production",
    "sandbox",
    "upc",
    "campaign",
    "group",
    "adgroup",
    "ads",
    "ad",
    "draft",
    "main",
    "backend",
}


def extract_campaign_enrichment_keyword_tokens(value: str) -> list[str]:
    tokens: list[str] = []
    seen_tokens = set()

    for token in re.findall(r"[0-9a-zа-я]+", normalize_match_text(value)):
        if token in SITE_THEME_STOPWORDS or token in CAMPAIGN_ENRICHMENT_KEYWORD_TECHNICAL_TOKENS or token.isdigit() or len(token) < 2:
            continue
        if token in seen_tokens:
            continue
        seen_tokens.add(token)
        tokens.append(token)

    return tokens


def normalize_campaign_enrichment_keyword_phrase(value: str) -> Optional[str]:
    normalized = normalize_match_text(clean_inline_text(value))
    if not normalized:
        return None

    tokens: list[str] = []
    for token in re.findall(r"[0-9a-zа-я]+", normalized):
        if token in CAMPAIGN_ENRICHMENT_KEYWORD_TECHNICAL_TOKENS or token.isdigit():
            continue
        if len(token) < 2 and token not in {"в", "из"}:
            continue
        tokens.append(token)

    if not tokens:
        return None

    return " ".join(tokens[:7])


def build_campaign_enrichment_keywords_for_group(
    theme: str,
    ad_group_name: str,
    current_keywords: Optional[list[str]] = None,
) -> list[str]:
    theme_text = clean_inline_text(theme)
    normalized_theme = normalize_campaign_enrichment_keyword_phrase(theme_text)
    if normalized_theme is None:
        return []

    normalized_group_name = normalize_match_text(ad_group_name)
    allowed_suffixes: list[str] = []
    if "в подарок" in normalized_group_name:
        allowed_suffixes.append("в подарок")
    if "для подарка" in normalized_group_name:
        allowed_suffixes.append("для подарка")
    if "для интерьера" in normalized_group_name:
        allowed_suffixes.append("для интерьера")
    if "для коллекции" in normalized_group_name:
        allowed_suffixes.append("для коллекции")

    candidate_phrases = [
        theme_text,
        f"{theme_text} фарфор",
    ]

    for suffix in allowed_suffixes:
        candidate_phrases.extend(
            [
                f"{theme_text} {suffix}",
                f"{theme_text} {suffix} фарфор",
            ]
        )

    existing_normalized = {
        normalize_match_text(keyword)
        for keyword in (current_keywords or [])
        if normalize_non_empty_string(keyword) is not None
    }

    prepared_keywords: list[str] = []
    seen_keywords = set()
    for candidate in candidate_phrases:
        normalized_keyword = normalize_campaign_enrichment_keyword_phrase(candidate)
        if normalized_keyword is None:
            continue
        normalized_key = normalize_match_text(normalized_keyword)
        if normalized_key in existing_normalized or normalized_key in seen_keywords:
            continue
        seen_keywords.add(normalized_key)
        prepared_keywords.append(normalized_keyword)
        if len(prepared_keywords) >= 5:
            break

    return prepared_keywords


def build_campaign_enrichment_keywords(
    ad_groups: list[dict],
    theme: str,
    current_keywords_by_ad_group: Optional[dict[int, list[str]]] = None,
) -> list[dict]:
    keywords_by_group = current_keywords_by_ad_group or {}
    result: list[dict] = []

    for ad_group in ad_groups:
        if not isinstance(ad_group, dict):
            continue
        ad_group_id = normalize_positive_int_id(ad_group.get("Id") or ad_group.get("id"))
        ad_group_name = normalize_non_empty_string(ad_group.get("Name") or ad_group.get("name"))
        if ad_group_id is None or ad_group_name is None:
            continue

        prepared_keywords = build_campaign_enrichment_keywords_for_group(
            theme=theme,
            ad_group_name=ad_group_name,
            current_keywords=keywords_by_group.get(ad_group_id),
        )
        if not prepared_keywords:
            continue

        result.append(
            {
                "ad_group_id": str(ad_group_id),
                "ad_group_name": ad_group_name,
                "keywords": prepared_keywords,
            }
        )

    return result



def extract_ad_extension_ids_from_raw_ad(raw_ad: dict) -> list[int]:
    if not isinstance(raw_ad, dict):
        return []
    text_ad = raw_ad.get("TextAd")
    if not isinstance(text_ad, dict):
        return []
    raw_extensions = text_ad.get("AdExtensions")
    if not isinstance(raw_extensions, list):
        return []

    ad_extension_ids: list[int] = []
    seen_ids = set()
    for raw_extension in raw_extensions:
        if not isinstance(raw_extension, dict):
            continue
        ad_extension_id = normalize_positive_int_id(raw_extension.get("AdExtensionId"))
        if ad_extension_id is None or ad_extension_id in seen_ids:
            continue
        seen_ids.add(ad_extension_id)
        ad_extension_ids.append(ad_extension_id)
    return ad_extension_ids


def collect_campaign_context_for_enrichment(client: YandexDirectClient, campaign_id: int) -> dict:
    campaign_result = client.get_campaign_details(campaign_id)
    campaigns = campaign_result.get("result", {}).get("Campaigns", [])
    if not campaigns:
        return {
            "ok": False,
            "status": 404,
            "payload": {
                "status": "error",
                "message": "campaign not found",
                "target": "production",
                "campaign_id": str(campaign_id),
                "raw": campaign_result,
            },
        }

    ad_groups_result = client.list_ad_groups(campaign_id)
    ad_groups = ad_groups_result.get("result", {}).get("AdGroups", [])
    ad_ids: list[int] = []
    ad_extension_ids_by_ad_id: dict[int, list[int]] = {}
    warnings: list[str] = []
    keywords_by_ad_group_id: dict[int, list[str]] = {}

    try:
        current_keywords_result = client.get_keywords(
            campaign_ids=[campaign_id],
            field_names=[
                "Id",
                "Keyword",
                "State",
                "Status",
                "ServingStatus",
                "AdGroupId",
                "CampaignId",
            ],
        )
    except YandexDirectClientError as e:
        warnings.append(f"Could not list current keywords for campaign {campaign_id}: {str(e)}")
    else:
        raw_keywords = current_keywords_result.get("result", {}).get("Keywords", [])
        for raw_keyword in raw_keywords:
            if not isinstance(raw_keyword, dict):
                continue
            ad_group_id = normalize_ad_group_id(raw_keyword.get("AdGroupId"))
            keyword_text = normalize_non_empty_string(raw_keyword.get("Keyword"))
            if ad_group_id is None or keyword_text is None or keyword_text == "---autotargeting":
                continue
            keywords_by_ad_group_id.setdefault(ad_group_id, []).append(keyword_text)

    for raw_ad_group in ad_groups:
        if not isinstance(raw_ad_group, dict):
            continue
        ad_group_id = normalize_ad_group_id(raw_ad_group.get("Id"))
        if ad_group_id is None:
            continue
        try:
            ads_result = client.list_ads(ad_group_id)
        except YandexDirectClientError as e:
            warnings.append(f"Could not list ads for ad_group {ad_group_id}: {str(e)}")
            continue
        raw_ads = ads_result.get("result", {}).get("Ads", [])
        for raw_ad in raw_ads:
            if not isinstance(raw_ad, dict):
                continue
            ad_id = normalize_ad_id(raw_ad.get("Id"))
            if ad_id is not None:
                ad_ids.append(ad_id)
                ad_extension_ids_by_ad_id[ad_id] = extract_ad_extension_ids_from_raw_ad(raw_ad)

    return {
        "ok": True,
        "status": 200,
        "payload": {
            "campaign": campaigns[0],
            "ad_groups": ad_groups,
            "ad_ids": ad_ids,
            "ad_extension_ids_by_ad_id": ad_extension_ids_by_ad_id,
            "keywords_by_ad_group_id": keywords_by_ad_group_id,
            "warnings": warnings,
        },
    }


def build_campaign_enrichment_preview_payload(client: YandexDirectClient, campaign_id: int, requested_theme: Optional[str] = None) -> dict:
    context_result = collect_campaign_context_for_enrichment(client, campaign_id)
    if not context_result["ok"]:
        return context_result

    context_payload = context_result["payload"]
    theme = normalize_non_empty_string(requested_theme) or infer_campaign_theme(
        context_payload["campaign"],
        context_payload["ad_groups"],
    )
    if theme is None:
        return {
            "ok": False,
            "status": 400,
            "payload": {
                "status": "error",
                "message": "theme is required because it could not be inferred from the current campaign",
                "target": "production",
                "campaign_id": str(campaign_id),
            },
        }

    sitelinks, sitelink_warnings = collect_safe_homepage_sitelinks(theme, limit=4)
    warnings = list(context_payload.get("warnings", []))
    warnings.extend(sitelink_warnings)

    return {
        "ok": True,
        "status": 200,
        "payload": {
            "campaign": context_payload["campaign"],
            "ad_groups": context_payload["ad_groups"],
            "ad_ids": context_payload["ad_ids"],
            "theme": theme,
            "preview": {
                "tracking_params": build_campaign_enrichment_tracking_params(theme),
                "sitelinks": sitelinks,
                "callouts": build_campaign_enrichment_callouts(theme),
                "keywords": build_campaign_enrichment_keywords(
                    context_payload["ad_groups"],
                    theme,
                    context_payload.get("keywords_by_ad_group_id"),
                ),
                "group_negative_keywords": build_campaign_enrichment_group_negative_keywords(
                    context_payload["ad_groups"],
                    theme,
                ),
                "negative_keywords": build_campaign_enrichment_negative_keywords(theme),
            },
            "warnings": warnings,
            "not_confirmed": build_campaign_enrichment_not_confirmed(),
        },
    }


def attach_sitelink_set_to_ads(client: YandexDirectClient, ad_ids: list[int], sitelink_set_id: int) -> dict:
    updated_ad_ids: list[str] = []
    errors: list[dict] = []

    for ad_id in ad_ids:
        try:
            result = client.update_text_ad_sitelink_set_id(ad_id=ad_id, sitelink_set_id=sitelink_set_id)
        except YandexDirectClientError as e:
            errors.append(
                {
                    "ad_id": str(ad_id),
                    "message": str(e),
                }
            )
            continue

        parsed = parse_update_result(result, "ad")
        if not parsed["ok"]:
            errors.append(
                {
                    "ad_id": str(ad_id),
                    "error": parsed["payload"],
                }
            )
            continue

        updated_ad_ids.append(parsed["payload"]["id"])

    return {
        "updated_ad_ids": updated_ad_ids,
        "errors": errors,
    }


def ensure_callouts_available(client: YandexDirectClient, callouts: list[str]) -> dict:
    try:
        current_callouts_result = client.get_callouts()
    except YandexDirectClientError as e:
        return {"ok": False, "status": 502, "payload": {"status": "error", "message": str(e)}}

    existing_by_text: dict[str, int] = {}
    raw_extensions = current_callouts_result.get("result", {}).get("AdExtensions", [])
    for raw_extension in raw_extensions:
        if not isinstance(raw_extension, dict):
            continue
        ad_extension_id = normalize_positive_int_id(raw_extension.get("Id"))
        if ad_extension_id is None:
            continue
        callout = raw_extension.get("Callout")
        if not isinstance(callout, dict):
            continue
        callout_text = normalize_callout_text_for_direct(callout.get("CalloutText"))
        if callout_text is None or callout_text in existing_by_text:
            continue
        existing_by_text[callout_text] = ad_extension_id

    ordered_items: list[dict] = []
    missing_callouts: list[str] = []
    seen_texts = set()
    for callout_text in callouts:
        normalized_text = normalize_callout_text_for_direct(callout_text)
        if normalized_text is None or normalized_text in seen_texts:
            continue
        seen_texts.add(normalized_text)
        existing_id = existing_by_text.get(normalized_text)
        if existing_id is not None:
            ordered_items.append({"id": existing_id, "text": normalized_text})
        else:
            missing_callouts.append(normalized_text)

    errors: list[dict] = []
    if missing_callouts:
        try:
            add_result = client.add_callouts(missing_callouts)
        except YandexDirectClientError as e:
            return {"ok": False, "status": 502, "payload": {"status": "error", "message": str(e)}}

        add_results = add_result.get("result", {}).get("AddResults", [])
        if not add_results:
            return {
                "ok": False,
                "status": 502,
                "payload": {
                    "status": "error",
                    "message": "empty AddResults from Yandex Direct for callouts",
                    "raw": add_result,
                },
            }

        for index, add_item in enumerate(add_results):
            callout_text = missing_callouts[index] if index < len(missing_callouts) else None
            if "Errors" in add_item:
                errors.append({"callout": callout_text, "errors": add_item["Errors"]})
                continue
            callout_id = normalize_positive_int_id(add_item.get("Id"))
            if callout_id is None or callout_text is None:
                errors.append({"callout": callout_text, "message": "missing callout id in Yandex Direct add response"})
                continue
            ordered_items.append({"id": callout_id, "text": callout_text})

    if errors and not ordered_items:
        return {
            "ok": False,
            "status": 400,
            "payload": {
                "status": "error",
                "message": "Yandex Direct rejected callout create",
                "errors": errors,
            },
        }

    ordered_items.sort(key=lambda item: callouts.index(item["text"]) if item["text"] in callouts else len(callouts))
    callout_ids = [item["id"] for item in ordered_items]
    return {
        "ok": True,
        "status": 200,
        "payload": {
            "callout_ids": callout_ids,
            "callout_items": [{"id": str(item["id"]), "text": item["text"]} for item in ordered_items],
            "errors": errors,
        },
    }


def attach_callouts_to_ads(
    client: YandexDirectClient,
    ad_ids: list[int],
    existing_ad_extension_ids_by_ad_id: dict[int, list[int]],
    callout_ids: list[int],
) -> dict:
    updated_ad_ids: list[str] = []
    errors: list[dict] = []
    warnings: list[str] = []

    for ad_id in ad_ids:
        existing_ids = existing_ad_extension_ids_by_ad_id.get(ad_id, [])
        combined_ids: list[int] = []
        seen_ids = set()
        for ad_extension_id in existing_ids + callout_ids:
            if ad_extension_id in seen_ids:
                continue
            seen_ids.add(ad_extension_id)
            combined_ids.append(ad_extension_id)

        if len(combined_ids) > 50:
            warnings.append(f"ad {ad_id} exceeds the 50-callout limit and was skipped")
            continue

        try:
            result = client.set_text_ad_callout_ids(
                ad_id=ad_id,
                ad_extension_ids=combined_ids,
            )
        except YandexDirectClientError as e:
            errors.append(
                {
                    "ad_id": str(ad_id),
                    "message": str(e),
                }
            )
            continue

        parsed = parse_update_result(result, "ad")
        if not parsed["ok"]:
            errors.append(
                {
                    "ad_id": str(ad_id),
                    "error": parsed["payload"],
                }
            )
            continue

        updated_ad_ids.append(str(ad_id))

    return {
        "updated_ad_ids": updated_ad_ids,
        "errors": errors,
        "warnings": warnings,
    }


def best_effort_cleanup_campaign_entities(client: YandexDirectClient, ad_ids: list[int], ad_group_ids: list[int]) -> dict:
    cleanup = {
        "ad_ids_attempted": [str(ad_id) for ad_id in ad_ids],
        "ad_group_ids_attempted": [str(ad_group_id) for ad_group_id in ad_group_ids],
        "delete_ads": build_cleanup_action_payload("skipped", results=[], raw=None),
        "delete_ad_groups": build_cleanup_action_payload("skipped", results=[], raw=None),
        "warnings": [],
    }

    if ad_ids:
        try:
            delete_ads_result = client.delete_ads(ad_ids)
            parsed_ads_delete = parse_action_results(delete_ads_result, "DeleteResults", "ads", "delete")
            if parsed_ads_delete["ok"]:
                cleanup["delete_ads"] = build_cleanup_action_payload(
                    "success",
                    results=parsed_ads_delete["payload"]["results"],
                    raw=delete_ads_result,
                )
            else:
                cleanup["delete_ads"] = build_cleanup_action_payload(
                    "error",
                    message=parsed_ads_delete["payload"]["message"],
                    results=parsed_ads_delete["payload"].get("results", []),
                    raw=delete_ads_result,
                )
                cleanup["warnings"].append("Some existing ads could not be deleted.")
                error_codes = collect_action_error_codes(parsed_ads_delete["payload"].get("results", []))
                if 8300 in error_codes:
                    cleanup["warnings"].append("Yandex Direct refused to delete part of the existing ads (Code 8300).")
        except YandexDirectClientError as e:
            cleanup["delete_ads"] = build_cleanup_action_payload("error", message=str(e), raw=None)
            cleanup["warnings"].append("Could not complete existing ads cleanup before replace.")

    if ad_group_ids:
        try:
            delete_ad_groups_result = client.delete_ad_groups(ad_group_ids)
            parsed_ad_groups_delete = parse_action_results(delete_ad_groups_result, "DeleteResults", "ad_groups", "delete")
            if parsed_ad_groups_delete["ok"]:
                cleanup["delete_ad_groups"] = build_cleanup_action_payload(
                    "success",
                    results=parsed_ad_groups_delete["payload"]["results"],
                    raw=delete_ad_groups_result,
                )
            else:
                cleanup["delete_ad_groups"] = build_cleanup_action_payload(
                    "error",
                    message=parsed_ad_groups_delete["payload"]["message"],
                    results=parsed_ad_groups_delete["payload"].get("results", []),
                    raw=delete_ad_groups_result,
                )
                cleanup["warnings"].append("Some existing ad groups could not be deleted.")
                error_codes = collect_action_error_codes(parsed_ad_groups_delete["payload"].get("results", []))
                if 8301 in error_codes:
                    cleanup["warnings"].append(
                        "Yandex Direct refused to delete part of the existing ad groups because they still contain ads or conditions (Code 8301)."
                    )
        except YandexDirectClientError as e:
            cleanup["delete_ad_groups"] = build_cleanup_action_payload("error", message=str(e), raw=None)
            cleanup["warnings"].append("Could not complete existing ad groups cleanup before replace.")

    return cleanup


def create_draft_structure_in_production_campaign(
    client: YandexDirectClient,
    campaign_id: int,
    draft_campaign: dict,
    region_ids: list[int],
) -> dict:
    created_ad_group_ids: list[str] = []
    created_ad_ids: list[str] = []
    created_ad_groups: list[dict] = []
    created_ads: list[dict] = []
    errors: list[dict] = []

    raw_ad_groups = draft_campaign.get("ad_groups")
    if not isinstance(raw_ad_groups, list) or not raw_ad_groups:
        return {
            "created_any": False,
            "created_ad_group_ids": created_ad_group_ids,
            "created_ad_ids": created_ad_ids,
            "created_ad_groups": created_ad_groups,
            "created_ads": created_ads,
            "errors": [
                {
                    "entity": "draft_campaign",
                    "message": "draft_campaign.ad_groups must be a non-empty array",
                }
            ],
        }

    for ad_group_index, raw_ad_group in enumerate(raw_ad_groups):
        if not isinstance(raw_ad_group, dict):
            errors.append(
                {
                    "entity": "ad_group",
                    "ad_group_index": ad_group_index,
                    "message": "draft ad_group must be an object",
                }
            )
            continue

        ad_group_name = normalize_non_empty_string(raw_ad_group.get("group_name"))
        if ad_group_name is None:
            errors.append(
                {
                    "entity": "ad_group",
                    "ad_group_index": ad_group_index,
                    "message": "draft ad_group group_name must be a non-empty string",
                }
            )
            continue

        negative_keywords = normalize_negative_keywords(raw_ad_group.get("negative_keywords"))
        if raw_ad_group.get("negative_keywords") is not None and negative_keywords is None:
            negative_keywords = []

        try:
            create_ad_group_result = client.add_unified_ad_group_production(
                campaign_id=campaign_id,
                name=ad_group_name,
                region_ids=region_ids,
                offer_retargeting="NO",
                negative_keywords=negative_keywords,
            )
        except YandexDirectClientError as e:
            errors.append(
                {
                    "entity": "ad_group",
                    "ad_group_index": ad_group_index,
                    "group_name": ad_group_name,
                    "message": str(e),
                }
            )
            continue

        parsed_ad_group = parse_add_result(create_ad_group_result, "ad_group")
        if not parsed_ad_group["ok"]:
            errors.append(
                {
                    "entity": "ad_group",
                    "ad_group_index": ad_group_index,
                    "group_name": ad_group_name,
                    "error": parsed_ad_group["payload"],
                }
            )
            continue

        created_ad_group_id = parsed_ad_group["payload"]["id"]
        created_ad_group_ids.append(created_ad_group_id)
        created_ad_groups.append(
            {
                "id": created_ad_group_id,
                "group_name": ad_group_name,
            }
        )

        raw_ads = raw_ad_group.get("ads")
        if not isinstance(raw_ads, list) or not raw_ads:
            errors.append(
                {
                    "entity": "ad_group",
                    "ad_group_index": ad_group_index,
                    "group_name": ad_group_name,
                    "message": "draft ad_group ads must be a non-empty array",
                }
            )
            continue

        for ad_index, raw_ad in enumerate(raw_ads):
            if not isinstance(raw_ad, dict):
                errors.append(
                    {
                        "entity": "ad",
                        "ad_group_index": ad_group_index,
                        "ad_index": ad_index,
                        "message": "draft ad must be an object",
                    }
                )
                continue

            normalize_draft_ad_images(raw_ad)
            title = normalize_non_empty_string(raw_ad.get("title"))
            text = normalize_non_empty_string(raw_ad.get("text"))
            href = normalize_draft_final_url(raw_ad.get("final_url"))
            ad_image_hash = normalize_non_empty_string(raw_ad.get("ad_image_hash"))

            if title is None or text is None or href is None:
                errors.append(
                    {
                        "entity": "ad",
                        "ad_group_index": ad_group_index,
                        "ad_index": ad_index,
                        "group_name": ad_group_name,
                        "message": "draft ad must contain non-empty title, text, final_url",
                    }
                )
                continue

            try:
                create_ad_result = client.add_text_ad_production(
                    ad_group_id=int(created_ad_group_id),
                    title=title,
                    text=text,
                    href=href,
                    ad_image_hash=ad_image_hash,
                )
            except YandexDirectClientError as e:
                errors.append(
                    {
                        "entity": "ad",
                        "ad_group_index": ad_group_index,
                        "ad_index": ad_index,
                        "group_name": ad_group_name,
                        "title": title,
                        "message": str(e),
                    }
                )
                continue

            parsed_ad = parse_add_result(create_ad_result, "ad")
            if not parsed_ad["ok"]:
                errors.append(
                    {
                        "entity": "ad",
                        "ad_group_index": ad_group_index,
                        "ad_index": ad_index,
                        "group_name": ad_group_name,
                        "title": title,
                        "error": parsed_ad["payload"],
                    }
                )
                continue

            created_ad_id = parsed_ad["payload"]["id"]
            created_ad_ids.append(created_ad_id)
            created_ads.append(
                {
                    "id": created_ad_id,
                    "ad_group_id": created_ad_group_id,
                    "title": title,
                }
            )

    return {
        "created_any": bool(created_ad_group_ids or created_ad_ids),
        "created_ad_group_ids": created_ad_group_ids,
        "created_ad_ids": created_ad_ids,
        "created_ad_groups": created_ad_groups,
        "created_ads": created_ads,
        "errors": errors,
    }


def extract_current_upc_strategy(campaign: dict) -> dict:
    bidding_strategy = campaign.get("UnifiedCampaign", {}).get("BiddingStrategy", {})
    pay_for_conversion = bidding_strategy.get("Search", {}).get("PayForConversion", {})

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


def build_campaign_stats_response(campaign_id: int, raw_date_from=None, raw_date_to=None):
    normalized_date_from = normalize_iso_date(raw_date_from) if raw_date_from is not None else None
    normalized_date_to = normalize_iso_date(raw_date_to) if raw_date_to is not None else None

    if raw_date_from is not None and normalized_date_from is None:
        return {"status": "error", "message": "date_from must be in YYYY-MM-DD format"}, 400

    if raw_date_to is not None and normalized_date_to is None:
        return {"status": "error", "message": "date_to must be in YYYY-MM-DD format"}, 400

    date_from = normalized_date_from or (date.today() - timedelta(days=7)).isoformat()
    date_to = normalized_date_to or date.today().isoformat()

    if date_from > date_to:
        return {"status": "error", "message": "date_from must be <= date_to"}, 400

    client = YandexDirectClient.for_target("production")

    try:
        report = client.get_campaign_stats_report(
            campaign_id=campaign_id,
            date_from=date_from,
            date_to=date_to,
        )
    except YandexDirectClientError as e:
        return (
            {"status": "error", "message": str(e), "target": "production", "campaign_id": str(campaign_id)},
            502,
        )

    try:
        goal_report = client.get_campaign_goal_stats_report(
            campaign_id=campaign_id,
            date_from=date_from,
            date_to=date_to,
            goal_id=DEFAULT_GOAL_ID,
        )
    except YandexDirectClientError as e:
        return (
            {
                "status": "error",
                "message": str(e),
                "target": "production",
                "campaign_id": str(campaign_id),
                "goal_id": str(DEFAULT_GOAL_ID),
            },
            502,
        )

    if report["report_status"] == "processing":
        return (
            {
                "status": "processing",
                "target": "production",
                "campaign_id": str(campaign_id),
                "date_from": date_from,
                "date_to": date_to,
                "request_id": report["request_id"],
                "retry_in": report["retry_in"],
                "rows": [],
            },
            report["status_code"],
        )

    rows = enrich_metrics(report["rows"])

    goal_rows = []
    goal_status = "unconfirmed"
    if goal_report["report_status"] == "processing":
        goal_status = "processing"
    else:
        goal_rows = goal_report.get("rows", [])
        goal_status = "ready" if goal_rows else "unconfirmed"

    return (
        {
            "status": "success",
            "target": "production",
            "campaign_id": str(campaign_id),
            "date_from": date_from,
            "date_to": date_to,
            "fields": [
                "Date",
                "CampaignId",
                "Clicks",
                "Impressions",
                "Cost",
                "AllGoalsConversions",
                "AllGoalsConversionRate",
                "AllGoalsCostPerConversion",
                "AvgCpc",
                "CTR",
                "CPC",
                "GoalCPA",
                "GoalConversionsConfirmed",
                "AllGoalsConversionsNumeric",
            ],
            "rows": rows,
            "all_goals_conversions_source": "aggregated_report_field",
            "goal_id": str(DEFAULT_GOAL_ID),
            "goal_attribution_model": "LC",
            "goal_report_status": goal_status,
            "goal_rows": goal_rows,
            "request_id": report["request_id"],
            "units": report["units"],
        },
        200,
    )



def build_search_terms_summary(rows: list[dict], processing: bool = False, analysis: Optional[dict] = None) -> str:
    if processing:
        return "Отчёт по реальным поисковым запросам ещё формируется."

    aggregated: dict[str, dict] = {}
    for row in rows:
        query = normalize_non_empty_string(row.get("query")) or "Без поискового запроса"
        item = aggregated.setdefault(
            query,
            {
                "query": query,
                "impressions": 0.0,
                "clicks": 0.0,
                "cost": 0.0,
                "conversions": 0.0,
            },
        )
        item["impressions"] += safe_float(row.get("impressions"))
        item["clicks"] += safe_float(row.get("clicks"))
        item["cost"] += safe_float(row.get("cost"))
        item["conversions"] += safe_float(row.get("conversions"))

    if not aggregated:
        return "По кампании не найдено реальных поисковых запросов за выбранный период."

    query_items = list(aggregated.values())
    top_clicks = sorted(query_items, key=lambda item: (item["clicks"], item["cost"]), reverse=True)[:3]
    top_cost = sorted(query_items, key=lambda item: (item["cost"], item["clicks"]), reverse=True)[:3]
    without_conversions = [item for item in query_items if item["clicks"] > 0 and item["conversions"] <= 0]

    top_clicks_text = ", ".join(
        f'{item["query"]} (клики: {format_rub_value(item["clicks"])})'
        for item in top_clicks
    ) or "нет данных"
    top_cost_text = ", ".join(
        f'{item["query"]} ({round(item["cost"], 2)})'
        for item in top_cost
    ) or "нет данных"

    if without_conversions:
        conversions_text = f"Запросов без конверсий: {format_rub_value(len(without_conversions))}."
    else:
        conversions_text = "Запросов без конверсий не найдено."

    summary = (
        f"Найдено {format_rub_value(len(query_items))} поисковых запросов. "
        f"Топ по кликам: {top_clicks_text}. "
        f"Топ по расходу: {top_cost_text}. "
        f"{conversions_text}"
    )

    if analysis is None:
        return summary

    negative_count = len(analysis.get("candidates_negative", []))
    waste_count = len(analysis.get("waste_queries", []))
    if negative_count > 0:
        negative_text = f"Есть {format_rub_value(negative_count)} кандидатов в минус-фразы."
    else:
        negative_text = "Явных кандидатов в минус-фразы пока нет."

    return (
        f"{summary} "
        f"Плохих запросов: {format_rub_value(negative_count)}. "
        f"С расходом без результата: {format_rub_value(waste_count)}. "
        f"{negative_text}"
    )


def build_search_terms_response(campaign_id: int, raw_date_from=None, raw_date_to=None, analyze: bool = False, target_cpa: Optional[float] = None):
    normalized_date_from = normalize_iso_date(raw_date_from) if raw_date_from is not None else None
    normalized_date_to = normalize_iso_date(raw_date_to) if raw_date_to is not None else None

    if raw_date_from is not None and normalized_date_from is None:
        return {"status": "error", "message": "date_from must be in YYYY-MM-DD format"}, 400

    if raw_date_to is not None and normalized_date_to is None:
        return {"status": "error", "message": "date_to must be in YYYY-MM-DD format"}, 400

    date_from = normalized_date_from or (date.today() - timedelta(days=6)).isoformat()
    date_to = normalized_date_to or date.today().isoformat()

    if date_from > date_to:
        return {"status": "error", "message": "date_from must be <= date_to"}, 400

    client = YandexDirectClient.for_target("production")

    try:
        report = client.get_search_terms_report(
            campaign_id=campaign_id,
            date_from=date_from,
            date_to=date_to,
        )
    except YandexDirectClientError as e:
        return (
            {"status": "error", "message": str(e), "target": "production", "campaign_id": str(campaign_id)},
            502,
        )

    if report["report_status"] == "processing":
        return (
            {
                "status": "processing",
                "campaign_id": str(campaign_id),
                "request_id": report["request_id"],
                "retry_in": report["retry_in"],
                "summary": build_search_terms_summary([], processing=True),
            },
            report["status_code"],
        )

    rows: list[dict] = []
    for raw_row in report.get("rows", []):
        if not isinstance(raw_row, dict):
            continue
        normalized_campaign_id = normalize_positive_int_id(raw_row.get("CampaignId")) or campaign_id
        normalized_ad_group_id = normalize_positive_int_id(raw_row.get("AdGroupId"))
        rows.append(
            {
                "date": normalize_non_empty_string(raw_row.get("Date")) or "",
                "campaign_id": str(normalized_campaign_id),
                "ad_group_id": str(normalized_ad_group_id) if normalized_ad_group_id is not None else "",
                "query": normalize_non_empty_string(raw_row.get("Query")) or "",
                "criteria": normalize_non_empty_string(raw_row.get("Criteria")) or "",
                "criteria_type": normalize_non_empty_string(raw_row.get("CriteriaType")) or "",
                "impressions": str(raw_row.get("Impressions", "")),
                "clicks": str(raw_row.get("Clicks", "")),
                "cost": str(raw_row.get("Cost", "")),
                "avg_cpc": str(raw_row.get("AvgCpc", "")),
                "conversions": str(raw_row.get("Conversions", "")),
            }
        )

    payload = {
        "status": "success",
        "campaign_id": str(campaign_id),
        "date_from": date_from,
        "date_to": date_to,
        "rows": rows,
        "request_id": report["request_id"],
        "units": report["units"],
    }

    if analyze:
        analysis = analyze_search_terms(rows, target_cpa=target_cpa)
        payload["analysis"] = analysis
        payload["summary"] = build_search_terms_summary(rows, analysis=analysis)
    else:
        payload["summary"] = build_search_terms_summary(rows)

    return payload, 200



def build_search_term_negative_preview_summary(candidates: list[dict], processing: bool = False) -> str:
    if processing:
        return "Отчёт по поисковым запросам ещё формируется. Ничего не применено."

    if not candidates:
        return "Кандидаты в минус-фразы не найдены. Ничего не применено."

    examples = "; ".join(
        f'{item["suggested_negative"]} <- {item["query"]} ({item["reason"]})'
        for item in candidates[:5]
    )
    return (
        f"Найдено {format_rub_value(len(candidates))} кандидатов в минус-фразы. "
        f"Примеры: {examples}. Ничего не применено."
    )


def build_search_term_negative_preview_response(campaign_id: int, raw_date_from=None, raw_date_to=None, raw_target_cpa=None):
    target_cpa = None
    if raw_target_cpa is not None:
        target_cpa = normalize_positive_number(raw_target_cpa)
        if target_cpa is None:
            return {"status": "error", "message": "target_cpa must be a positive number when provided"}, 400

    payload, status_code = build_search_terms_response(
        campaign_id=campaign_id,
        raw_date_from=raw_date_from,
        raw_date_to=raw_date_to,
        analyze=True,
        target_cpa=target_cpa,
    )

    if payload.get("status") == "error":
        return payload, status_code

    if payload.get("status") == "processing":
        return (
            {
                "status": "processing",
                "campaign_id": payload.get("campaign_id", str(campaign_id)),
                "request_id": payload.get("request_id", ""),
                "retry_in": payload.get("retry_in", ""),
                "summary": build_search_term_negative_preview_summary([], processing=True),
            },
            status_code,
        )

    rows = payload.get("rows", [])
    analysis = payload.get("analysis", {})
    candidates = build_negative_preview_candidates(rows, analysis, target_cpa=target_cpa)

    return (
        {
            "status": "success",
            "campaign_id": payload.get("campaign_id", str(campaign_id)),
            "date_from": payload.get("date_from", ""),
            "date_to": payload.get("date_to", ""),
            "candidates": candidates,
            "summary": build_search_term_negative_preview_summary(candidates),
        },
        200,
    )



def collect_campaign_negative_keyword_state(client: YandexDirectClient, campaign_id: int) -> dict:
    current_result = client.get_campaign_details(campaign_id)
    campaigns = current_result.get("result", {}).get("Campaigns", [])
    if not campaigns:
        return {
            "ok": False,
            "status": 404,
            "payload": {
                "status": "error",
                "message": "campaign not found",
                "target": "production",
                "campaign_id": str(campaign_id),
                "raw": current_result,
            },
        }

    campaign = campaigns[0]
    shared_set_ids = []
    for item in campaign.get("UnifiedCampaign", {}).get("NegativeKeywordSharedSetIds", {}).get("Items", []):
        normalized_item = normalize_positive_int_id(item)
        if normalized_item is not None:
            shared_set_ids.append(normalized_item)

    warnings: list[str] = []
    existing_negative_keywords: list[str] = []
    seen_negative_keywords = set()

    if shared_set_ids:
        try:
            shared_sets_result = client.get_negative_keyword_shared_sets(ids=shared_set_ids)
        except YandexDirectClientError as e:
            warnings.append(f"Could not confirm current campaign negative keywords: {str(e)}")
        else:
            raw_shared_sets = shared_sets_result.get("result", {}).get("NegativeKeywordSharedSets", [])
            for raw_shared_set in raw_shared_sets:
                if not isinstance(raw_shared_set, dict):
                    continue
                normalized_keywords = normalize_negative_keywords(raw_shared_set.get("NegativeKeywords")) or []
                for keyword in normalized_keywords:
                    normalized_keyword = normalize_non_empty_string(keyword)
                    if normalized_keyword is None or normalized_keyword in seen_negative_keywords:
                        continue
                    seen_negative_keywords.add(normalized_keyword)
                    existing_negative_keywords.append(normalized_keyword)

    return {
        "ok": True,
        "status": 200,
        "payload": {
            "campaign": campaign,
            "shared_set_ids": shared_set_ids,
            "existing_negative_keywords": existing_negative_keywords,
            "warnings": warnings,
        },
    }


def build_apply_search_term_negative_summary(applied_keywords: list[str], warnings: list[str], errors: list[dict], processing: bool = False) -> str:
    if processing:
        return "Отчёт по поисковым запросам ещё формируется. Ничего не применено."

    if applied_keywords:
        examples = ", ".join(applied_keywords[:5])
        return (
            f"Применено {format_rub_value(len(applied_keywords))} минус-фраз. "
            f"Примеры: {examples}."
        )

    if errors:
        return "Минус-фразы не применены из-за ошибки."

    if warnings:
        return "Новых минус-фраз для применения не найдено."

    return "Минус-фразы не применены."


def build_apply_search_term_negatives_response(campaign_id: int, raw_date_from=None, raw_date_to=None, raw_target_cpa=None):
    target_cpa = None
    if raw_target_cpa is not None:
        target_cpa = normalize_positive_number(raw_target_cpa)
        if target_cpa is None:
            return {"status": "error", "message": "target_cpa must be a positive number when provided"}, 400

    client = YandexDirectClient.for_target("production")

    preview_payload, preview_status_code = build_search_term_negative_preview_response(
        campaign_id=campaign_id,
        raw_date_from=raw_date_from,
        raw_date_to=raw_date_to,
        raw_target_cpa=raw_target_cpa,
    )

    if preview_payload.get("status") == "error":
        return preview_payload, preview_status_code

    if preview_payload.get("status") == "processing":
        preview_payload["summary"] = build_apply_search_term_negative_summary([], [], [], processing=True)
        return preview_payload, preview_status_code

    state_result = collect_campaign_negative_keyword_state(client=client, campaign_id=campaign_id)
    if not state_result["ok"]:
        return state_result["payload"], state_result["status"]

    state_payload = state_result["payload"]
    warnings = list(state_payload.get("warnings", []))
    errors: list[dict] = []

    existing_negative_keywords = set(state_payload.get("existing_negative_keywords", []))
    shared_set_ids = list(state_payload.get("shared_set_ids", []))
    prepared_negative_keywords: list[str] = []
    seen_negative_keywords = set()

    for item in preview_payload.get("candidates", []):
        if not isinstance(item, dict):
            continue
        if item.get("level") != "campaign":
            continue
        suggested_negative = normalize_non_empty_string(item.get("suggested_negative"))
        query = normalize_non_empty_string(item.get("query"))
        if suggested_negative is None:
            continue
        if query is not None and normalize_match_text(suggested_negative) == normalize_match_text(query):
            warnings.append(f"Skipped full-query negative candidate '{suggested_negative}'.")
            continue
        if suggested_negative in existing_negative_keywords or suggested_negative in seen_negative_keywords:
            continue
        seen_negative_keywords.add(suggested_negative)
        prepared_negative_keywords.append(suggested_negative)

    if not prepared_negative_keywords:
        return (
            {
                "status": "success",
                "campaign_id": str(campaign_id),
                "applied": {
                    "negative_keywords": [],
                    "source": "search_terms_preview",
                    "count": 0,
                },
                "warnings": warnings,
                "errors": errors,
                "summary": build_apply_search_term_negative_summary([], warnings, errors),
            },
            200,
        )

    if len(shared_set_ids) >= 3:
        warnings.append("NegativeKeywordSharedSetIds limit prevented applying new search-term negatives to the campaign.")
        errors.append({"step": "attach_negative_keywords", "message": "NegativeKeywordSharedSetIds supports at most 3 items"})
        return (
            {
                "status": "error",
                "campaign_id": str(campaign_id),
                "applied": {
                    "negative_keywords": [],
                    "source": "search_terms_preview",
                    "count": 0,
                },
                "warnings": warnings,
                "errors": errors,
                "summary": build_apply_search_term_negative_summary([], warnings, errors),
            },
            400,
        )

    negative_set_name = f"OpenClaw Search Terms {campaign_id} {datetime.utcnow().strftime('%Y%m%d%H%M%S')}"

    try:
        negative_set_result = client.add_negative_keyword_shared_set(
            name=negative_set_name,
            negative_keywords=prepared_negative_keywords,
        )
    except YandexDirectClientError as e:
        errors.append({"step": "create_negative_keywords", "message": str(e)})
        return (
            {
                "status": "error",
                "campaign_id": str(campaign_id),
                "applied": {
                    "negative_keywords": [],
                    "source": "search_terms_preview",
                    "count": 0,
                },
                "warnings": warnings,
                "errors": errors,
                "summary": build_apply_search_term_negative_summary([], warnings, errors),
            },
            502,
        )

    parsed_negative_set = parse_add_result(negative_set_result, "negative_keyword_shared_set")
    if not parsed_negative_set["ok"]:
        errors.append({"step": "create_negative_keywords", "error": parsed_negative_set["payload"]})
        return (
            {
                "status": "error",
                "campaign_id": str(campaign_id),
                "applied": {
                    "negative_keywords": [],
                    "source": "search_terms_preview",
                    "count": 0,
                },
                "warnings": warnings,
                "errors": errors,
                "summary": build_apply_search_term_negative_summary([], warnings, errors),
            },
            parsed_negative_set["status"],
        )

    new_shared_set_id = int(parsed_negative_set["payload"]["id"])
    updated_shared_set_ids = shared_set_ids + [new_shared_set_id]

    if len(updated_shared_set_ids) > 3:
        warnings.append("NegativeKeywordSharedSetIds limit prevented applying new search-term negatives to the campaign.")
        errors.append({"step": "attach_negative_keywords", "message": "NegativeKeywordSharedSetIds supports at most 3 items"})
        return (
            {
                "status": "error",
                "campaign_id": str(campaign_id),
                "applied": {
                    "negative_keywords": [],
                    "source": "search_terms_preview",
                    "count": 0,
                },
                "warnings": warnings,
                "errors": errors,
                "summary": build_apply_search_term_negative_summary([], warnings, errors),
            },
            400,
        )

    try:
        attach_result = client.update_campaign_negative_keyword_shared_set_ids(
            campaign_id=campaign_id,
            shared_set_ids=updated_shared_set_ids,
        )
    except YandexDirectClientError as e:
        errors.append({"step": "attach_negative_keywords", "message": str(e)})
        return (
            {
                "status": "error",
                "campaign_id": str(campaign_id),
                "applied": {
                    "negative_keywords": [],
                    "source": "search_terms_preview",
                    "count": 0,
                },
                "warnings": warnings,
                "errors": errors,
                "summary": build_apply_search_term_negative_summary([], warnings, errors),
            },
            502,
        )

    parsed_attach = parse_update_result(attach_result, "campaign")
    if not parsed_attach["ok"]:
        errors.append({"step": "attach_negative_keywords", "error": parsed_attach["payload"]})
        return (
            {
                "status": "error",
                "campaign_id": str(campaign_id),
                "applied": {
                    "negative_keywords": [],
                    "source": "search_terms_preview",
                    "count": 0,
                },
                "warnings": warnings,
                "errors": errors,
                "summary": build_apply_search_term_negative_summary([], warnings, errors),
            },
            parsed_attach["status"],
        )

    return (
        {
            "status": "success",
            "campaign_id": str(campaign_id),
            "applied": {
                "negative_keywords": prepared_negative_keywords,
                "source": "search_terms_preview",
                "count": len(prepared_negative_keywords),
            },
            "warnings": warnings,
            "errors": errors,
            "summary": build_apply_search_term_negative_summary(prepared_negative_keywords, warnings, errors),
        },
        200,
    )


def build_campaign_analysis_response(stats_payload: dict, focus: Optional[str] = None) -> dict:
    campaign_id = stats_payload.get("campaign_id")
    date_from = stats_payload.get("date_from")
    date_to = stats_payload.get("date_to")

    if stats_payload.get("status") == "processing":
        analysis = {
            "status": "processing",
            "campaign_id": campaign_id,
            "date_from": date_from,
            "date_to": date_to,
            "analysis_mode": "report_processing",
            "summary": {
                "message": "Отчёт по кампании ещё формируется."
            },
            "traffic": {
                "status": "processing"
            },
            "conversions": {
                "goal_report_status": None,
                "goal_conversions_confirmed": False,
                "note": "Данные по кампании ещё недоступны."
            },
            "conclusion": [
                "Отчёт по кампании ещё формируется."
            ],
            "recommendations": [
                {
                    "title": "Повторить анализ позже",
                    "reason": "Основной отчёт кампании ещё формируется.",
                    "kind": "confirmed",
                }
            ],
        }
        if focus == "low_conversion":
            analysis["low_conversion_analysis"] = {
                "confirmed_facts": [
                    "Основной отчёт кампании ещё формируется."
                ],
                "possible_causes": [
                    {
                        "cause": "Причину низкой конверсии пока нельзя определить.",
                        "confidence": "high",
                        "basis": "Нет готового основного отчёта кампании.",
                    }
                ],
                "next_checks": [
                    "Повторить анализ после готовности основного отчёта кампании."
                ],
            }
        return analysis

    rows = stats_payload.get("rows", [])
    goal_rows = stats_payload.get("goal_rows", [])
    goal_report_status = stats_payload.get("goal_report_status")

    impressions = sum(safe_float(row.get("Impressions")) for row in rows)
    clicks = sum(safe_float(row.get("Clicks")) for row in rows)
    cost = sum(safe_float(row.get("Cost")) for row in rows)
    all_goals_conversions = sum(safe_float(row.get("AllGoalsConversionsNumeric")) for row in rows)

    ctr = (clicks / impressions * 100) if impressions > 0 else 0.0
    cpc = (cost / clicks) if clicks > 0 else 0.0

    goal_conversions_confirmed = any(bool(row.get("GoalConversionsConfirmed")) for row in rows)
    goal_conversions = sum(safe_float(row.get("Conversions")) for row in goal_rows)

    analysis_mode = "goal_efficiency" if goal_report_status == "ready" and goal_conversions_confirmed else "traffic_only"

    goal_cpa = None
    if analysis_mode == "goal_efficiency" and goal_conversions > 0:
        goal_cpa = round(cost / goal_conversions, 4)

    conversions_note = None
    if goal_report_status == "processing":
        conversions_note = "Отчёт по целевой цели ещё формируется."
    elif goal_report_status != "ready":
        conversions_note = "По целевой цели стратегии данные отсутствуют; анализ целевой эффективности недоступен."
    elif not goal_conversions_confirmed:
        conversions_note = "GoalConversionsConfirmed != true; анализ GoalCPA не выполняется."
    elif goal_conversions <= 0:
        conversions_note = "Подтверждённые конверсии по целевой цели не зафиксированы."
    else:
        conversions_note = "Подтверждённые данные по целевой цели доступны."

    conclusion = [
        f"За период получено {format_rub_value(impressions)} показов, {format_rub_value(clicks)} кликов и расход {round(cost, 4)}."
    ]
    if goal_report_status == "processing":
        conclusion.append("Отчёт по целевой цели ещё формируется.")
    elif goal_report_status != "ready":
        conclusion.append("По целевой цели стратегии данные отсутствуют; анализ целевой эффективности недоступен.")
    elif not goal_conversions_confirmed:
        conclusion.append("Текущий stats flow не подтверждает GoalConversionsConfirmed=true; анализ GoalCPA не выполняется.")
    elif goal_cpa is not None:
        conclusion.append(f"Подтверждённый GoalCPA за период: {goal_cpa}.")

    recommendations = []
    if goal_report_status == "processing":
        recommendations.append(
            {
                "title": "Дождаться отчёта по цели",
                "reason": "Отчёт по целевой цели ещё формируется.",
                "kind": "confirmed",
            }
        )
    elif goal_report_status != "ready":
        recommendations.append(
            {
                "title": "Проверить данные по целевой цели",
                "reason": "По целевой цели стратегии данные отсутствуют; анализ целевой эффективности недоступен.",
                "kind": "confirmed",
            }
        )
        recommendations.append(
            {
                "title": "Проверить связку Метрики и цели",
                "reason": f"Нужно проверить, поступают ли данные по цели {DEFAULT_GOAL_ID} и корректно ли она используется стратегией.",
                "kind": "hypothesis",
            }
        )
    elif not goal_conversions_confirmed:
        recommendations.append(
            {
                "title": "Подтвердить конверсии по цели",
                "reason": "Текущий stats flow не подтверждает GoalConversionsConfirmed=true; менять target CPA по этим данным нельзя.",
                "kind": "confirmed",
            }
        )

    if clicks == 0 and impressions > 0:
        recommendations.append(
            {
                "title": "Проверить кликабельность",
                "reason": "Есть показы без кликов; конверсионное качество трафика пока нельзя оценить.",
                "kind": "hypothesis",
            }
        )
    elif clicks > 0 and all_goals_conversions == 0:
        recommendations.append(
            {
                "title": "Проверить посадочную страницу и релевантность трафика",
                "reason": "Есть трафик и расход, но агрегированные all-goals conversions не зафиксированы даже как справочная метрика.",
                "kind": "hypothesis",
            }
        )

    analysis = {
        "status": "success",
        "campaign_id": campaign_id,
        "date_from": date_from,
        "date_to": date_to,
        "analysis_mode": analysis_mode,
        "summary": {
            "goal_report_status": goal_report_status,
            "goal_conversions_confirmed": goal_conversions_confirmed,
            "all_goals_conversions_reference": format_rub_value(all_goals_conversions),
        },
        "traffic": {
            "impressions": format_rub_value(impressions),
            "clicks": format_rub_value(clicks),
            "cost": round(cost, 4),
            "ctr": round(ctr, 4),
            "cpc": round(cpc, 4),
        },
        "conversions": {
            "goal_report_status": goal_report_status,
            "goal_conversions_confirmed": goal_conversions_confirmed,
            "goal_conversions": format_rub_value(goal_conversions) if goal_rows else None,
            "goal_cpa": goal_cpa,
            "all_goals_conversions_reference": format_rub_value(all_goals_conversions),
            "note": conversions_note,
        },
        "conclusion": conclusion,
        "recommendations": recommendations,
    }

    if focus == "low_conversion":
        confirmed_facts = [
            f"Показы: {format_rub_value(impressions)}.",
            f"Клики: {format_rub_value(clicks)}.",
            f"Расход: {round(cost, 4)}.",
            f"Справочная all-goals conversions: {format_rub_value(all_goals_conversions)}.",
        ]

        if goal_report_status == "processing":
            confirmed_facts.append("Отчёт по целевой цели ещё формируется.")
        elif goal_report_status != "ready":
            confirmed_facts.append("По целевой цели стратегии данные отсутствуют; анализ целевой эффективности недоступен.")
        elif not goal_conversions_confirmed:
            confirmed_facts.append("GoalConversionsConfirmed != true; анализ GoalCPA не выполняется.")
        else:
            confirmed_facts.append(f"Подтверждённые конверсии по цели: {format_rub_value(goal_conversions)}.")

        possible_causes = []
        if goal_report_status == "processing":
            possible_causes.append(
                {
                    "cause": "Причину низкой конверсии по целевой цели пока нельзя подтвердить.",
                    "confidence": "high",
                    "basis": "Отчёт по целевой цели ещё формируется.",
                }
            )
        elif goal_report_status != "ready":
            possible_causes.append(
                {
                    "cause": "Причину низкой конверсии по целевой цели нельзя подтвердить из-за отсутствия данных по цели.",
                    "confidence": "high",
                    "basis": "Goal report не готов к анализу целевой эффективности.",
                }
            )
        elif not goal_conversions_confirmed:
            possible_causes.append(
                {
                    "cause": "Причину низкой конверсии по целевой цели нельзя подтвердить, потому что GoalConversionsConfirmed != true.",
                    "confidence": "high",
                    "basis": "Текущий stats flow не подтверждает целевые конверсии для расчёта GoalCPA.",
                }
            )

        if clicks == 0 and impressions > 0:
            possible_causes.append(
                {
                    "cause": "Недостаточно кликов для оценки конверсии.",
                    "confidence": "high",
                    "basis": "Есть показы, но кликов нет.",
                }
            )
        elif clicks > 0 and all_goals_conversions == 0:
            possible_causes.append(
                {
                    "cause": "Трафик может быть нерелевантным или посадочная страница не доводит пользователя до действий.",
                    "confidence": "medium",
                    "basis": "Есть клики и расход, но даже агрегированные all-goals conversions равны 0.",
                }
            )

        next_checks = []
        if goal_report_status == "processing":
            next_checks.append("Повторить анализ после готовности отчёта по целевой цели.")
        else:
            next_checks.append(f"Проверить, поступают ли данные по цели {DEFAULT_GOAL_ID} в Метрику.")
        if clicks == 0 and impressions > 0:
            next_checks.append("Проверить объявления, поисковые запросы и кликабельность.")
        else:
            next_checks.append("Проверить посадочную страницу, поисковые запросы и соответствие оффера трафику.")

        analysis["low_conversion_analysis"] = {
            "confirmed_facts": confirmed_facts,
            "possible_causes": possible_causes,
            "next_checks": next_checks,
        }

    return analysis


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
                self._send_json({"status": "success", "valid": True, "errors": []}, 200)
            else:
                self._send_json({"status": "error", "valid": False, "errors": errors}, 400)
            return

        if self.path == "/create_campaign":
            is_valid, errors = validate_campaign(data)
            if not is_valid:
                self._send_json({"status": "error", "valid": False, "errors": errors}, 400)
                return

            target = resolve_target(data)
            if target not in ALLOWED_TARGETS:
                self._send_json({"status": "error", "message": "target must be 'sandbox' or 'production'"}, 400)
                return

            confirm = resolve_confirm(data)
            start_date = resolve_start_date(data)

            if target == "production" and not confirm:
                self._send_json(
                    {"status": "error", "message": "production create requires explicit confirm=true", "target": "production"},
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
                self._send_json({"status": "error", "message": str(e), "target": target}, 502)
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
                self._send_json({"status": "error", "message": "target must be 'sandbox' or 'production'"}, 400)
                return

            campaign_id = normalize_campaign_id(data.get("campaign_id"))
            if campaign_id is None:
                self._send_json({"status": "error", "message": "campaign_id must be a positive integer or numeric string"}, 400)
                return

            campaign_name = data.get("campaign_name")
            if campaign_name is not None:
                if not isinstance(campaign_name, str) or not campaign_name.strip():
                    self._send_json({"status": "error", "message": "campaign_name must be a non-empty string when provided"}, 400)
                    return
                campaign_name = campaign_name.strip()

            target_cpa_rub = normalize_positive_number(data.get("target_cpa_rub"))
            weekly_budget_rub = normalize_positive_number(data.get("weekly_budget_rub"))
            metrica_goal_id = normalize_campaign_id(data.get("metrica_goal_id"))

            strategy_update_requested = any(key in data for key in ("target_cpa_rub", "weekly_budget_rub", "metrica_goal_id"))

            if "target_cpa_rub" in data and target_cpa_rub is None:
                self._send_json({"status": "error", "message": "target_cpa_rub must be a positive number when provided"}, 400)
                return

            if "weekly_budget_rub" in data and weekly_budget_rub is None:
                self._send_json({"status": "error", "message": "weekly_budget_rub must be a positive number when provided"}, 400)
                return

            if "metrica_goal_id" in data and metrica_goal_id is None:
                self._send_json(
                    {"status": "error", "message": "metrica_goal_id must be a positive integer or numeric string when provided"},
                    400,
                )
                return

            if campaign_name is None and not strategy_update_requested:
                self._send_json({"status": "error", "message": "nothing to update: provide campaign_name and/or strategy fields"}, 400)
                return

            confirm = resolve_confirm(data)
            if target == "production" and not confirm:
                self._send_json(
                    {"status": "error", "message": "production campaign update requires explicit confirm=true", "target": "production"},
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
                        weekly_budget_rub if weekly_budget_rub is not None else micros_to_rub(current_weekly_budget_micros)
                    )

                    if final_weekly_budget_rub < final_cpa_rub * 20:
                        self._send_json(
                            {"status": "error", "message": f"weekly_budget_rub must be >= target_cpa_rub * 20 ({final_cpa_rub * 20})"},
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
                        client.update_campaign_sandbox(campaign_id=campaign_id, name=campaign_name)
                        if target == "sandbox"
                        else client.update_campaign_production(campaign_id=campaign_id, name=campaign_name)
                    )

            except YandexDirectClientError as e:
                self._send_json(
                    {"status": "error", "message": str(e), "target": target, "campaign_id": str(campaign_id)},
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
                    "metrica_goal_id": metrica_goal_id if metrica_goal_id is not None else current_goal_id,
                    "target_cpa_rub": target_cpa_rub if target_cpa_rub is not None else micros_to_rub(current_cpa_micros),
                    "weekly_budget_rub": (
                        weekly_budget_rub if weekly_budget_rub is not None else micros_to_rub(current_weekly_budget_micros)
                    ),
                }

            self._send_json(response_payload, 200)
            return

        if self.path == "/propose_campaign_update":
            campaign_id = normalize_campaign_id(data.get("campaign_id"))
            if campaign_id is None:
                self._send_json({"status": "error", "message": "campaign_id must be a positive integer or numeric string"}, 400)
                return

            change_request = data.get("change_request")
            if not isinstance(change_request, dict):
                self._send_json({"status": "error", "message": "change_request must be an object"}, 400)
                return

            change_type = change_request.get("type")
            if change_type not in {"update_cpa", "update_weekly_budget"}:
                self._send_json({"status": "error", "message": "supported change_request.type values are update_cpa and update_weekly_budget"}, 400)
                return

            change_value = normalize_positive_number(change_request.get("value"))
            if change_value is None:
                self._send_json({"status": "error", "message": "change_request.value must be a positive number"}, 400)
                return

            client = YandexDirectClient.for_target("production")

            try:
                current_result = client.get_campaign_details(campaign_id)
            except YandexDirectClientError as e:
                self._send_json(
                    {"status": "error", "message": str(e), "target": "production", "campaign_id": str(campaign_id)},
                    502,
                )
                return

            campaigns = current_result.get("result", {}).get("Campaigns", [])
            if not campaigns:
                self._send_json(
                    {"status": "error", "message": "campaign not found", "target": "production", "campaign_id": str(campaign_id), "raw": current_result},
                    404,
                )
                return

            current_strategy = extract_current_upc_strategy(campaigns[0])
            current_cpa_rub = micros_to_rub(current_strategy["cpa_micros"])
            current_weekly_budget_rub = micros_to_rub(current_strategy["weekly_budget_micros"])

            if change_type == "update_cpa":
                if float(change_value) == float(current_cpa_rub):
                    self._send_json({"status": "error", "message": "proposal does not change campaign cpa"}, 400)
                    return

                if current_weekly_budget_rub < change_value * 20:
                    self._send_json(
                        {"status": "error", "message": f"weekly_budget_rub must be >= target_cpa_rub * 20 ({change_value * 20})"},
                        400,
                    )
                    return

                proposal_changes = [
                    {
                        "field": "Cpa",
                        "old_value": format_rub_value(current_cpa_rub),
                        "new_value": format_rub_value(change_value),
                    }
                ]
                summary = (
                    f"Снижение CPA с {format_rub_value(current_cpa_rub)} до {format_rub_value(change_value)}"
                    if change_value < current_cpa_rub
                    else f"Повышение CPA с {format_rub_value(current_cpa_rub)} до {format_rub_value(change_value)}"
                )
                update_payload = {
                    "campaign_id": str(campaign_id),
                    "target_cpa_rub": format_rub_value(change_value),
                }
            else:
                if float(change_value) == float(current_weekly_budget_rub):
                    self._send_json({"status": "error", "message": "proposal does not change campaign weekly budget"}, 400)
                    return

                if change_value < current_cpa_rub * 20:
                    self._send_json(
                        {"status": "error", "message": f"weekly_budget_rub must be >= target_cpa_rub * 20 ({current_cpa_rub * 20})"},
                        400,
                    )
                    return

                proposal_changes = [
                    {
                        "field": "WeeklySpendLimit",
                        "old_value": format_rub_value(current_weekly_budget_rub),
                        "new_value": format_rub_value(change_value),
                    }
                ]
                summary = (
                    f"Снижение недельного бюджета с {format_rub_value(current_weekly_budget_rub)} до {format_rub_value(change_value)}"
                    if change_value < current_weekly_budget_rub
                    else f"Увеличение недельного бюджета с {format_rub_value(current_weekly_budget_rub)} до {format_rub_value(change_value)}"
                )
                update_payload = {
                    "campaign_id": str(campaign_id),
                    "weekly_budget_rub": format_rub_value(change_value),
                }

            proposal = {
                "campaign_id": str(campaign_id),
                "target": "production",
                "change_request": {
                    "type": change_type,
                    "value": format_rub_value(change_value),
                },
                "changes": proposal_changes,
                "summary": summary,
                "update_payload": update_payload,
            }

            state = deep_copy_json(RUNTIME_STATE)
            state["session_mode"] = "awaiting_confirm_update"
            state["last_plan"] = None
            state["last_proposal"] = proposal
            proposal_history = state.get("proposal_history", [])
            if not isinstance(proposal_history, list):
                proposal_history = []
            proposal_history.append(proposal)
            state["proposal_history"] = proposal_history
            save_runtime_state(state)

            self._send_json(
                {
                    "status": "success",
                    "proposal": {
                        "campaign_id": proposal["campaign_id"],
                        "changes": proposal["changes"],
                        "summary": proposal["summary"],
                    },
                    "session_mode": state["session_mode"],
                },
                200,
            )
            return

        if self.path == "/plan_campaign_update":
            campaign_id = normalize_campaign_id(data.get("campaign_id"))
            if campaign_id is None:
                self._send_json({"status": "error", "message": "campaign_id must be a positive integer or numeric string"}, 400)
                return

            raw_changes = data.get("changes")
            if not isinstance(raw_changes, list) or not raw_changes:
                self._send_json({"status": "error", "message": "changes must be a non-empty array"}, 400)
                return

            client = YandexDirectClient.for_target("production")

            try:
                current_result = client.get_campaign_details(campaign_id)
            except YandexDirectClientError as e:
                self._send_json(
                    {"status": "error", "message": str(e), "target": "production", "campaign_id": str(campaign_id)},
                    502,
                )
                return

            campaigns = current_result.get("result", {}).get("Campaigns", [])
            if not campaigns:
                self._send_json(
                    {"status": "error", "message": "campaign not found", "target": "production", "campaign_id": str(campaign_id), "raw": current_result},
                    404,
                )
                return

            current_strategy = extract_current_upc_strategy(campaigns[0])
            planned_cpa_rub = micros_to_rub(current_strategy["cpa_micros"])
            planned_weekly_budget_rub = micros_to_rub(current_strategy["weekly_budget_micros"])

            plan_changes = []
            summary_parts = []
            update_payload = {
                "campaign_id": str(campaign_id),
            }

            for raw_change in raw_changes:
                if not isinstance(raw_change, dict):
                    self._send_json({"status": "error", "message": "each changes item must be an object"}, 400)
                    return

                change_type = raw_change.get("type")
                if change_type not in {"update_cpa", "update_weekly_budget"}:
                    self._send_json({"status": "error", "message": "supported changes types are update_cpa and update_weekly_budget"}, 400)
                    return

                change_value = normalize_positive_number(raw_change.get("value"))
                if change_value is None:
                    self._send_json({"status": "error", "message": "each changes value must be a positive number"}, 400)
                    return

                if change_type == "update_cpa":
                    old_value = planned_cpa_rub
                    if float(change_value) == float(old_value):
                        self._send_json({"status": "error", "message": "plan does not change campaign cpa"}, 400)
                        return

                    if planned_weekly_budget_rub < change_value * 20:
                        self._send_json(
                            {"status": "error", "message": f"weekly_budget_rub must be >= target_cpa_rub * 20 ({change_value * 20})"},
                            400,
                        )
                        return

                    plan_changes.append(
                        {
                            "type": "update_cpa",
                            "field": "Cpa",
                            "old_value": format_rub_value(old_value),
                            "new_value": format_rub_value(change_value),
                        }
                    )
                    summary_parts.append(f"CPA {format_rub_value(old_value)} -> {format_rub_value(change_value)}")
                    planned_cpa_rub = change_value
                    update_payload["target_cpa_rub"] = format_rub_value(change_value)
                else:
                    old_value = planned_weekly_budget_rub
                    if float(change_value) == float(old_value):
                        self._send_json({"status": "error", "message": "plan does not change campaign weekly budget"}, 400)
                        return

                    if change_value < planned_cpa_rub * 20:
                        self._send_json(
                            {"status": "error", "message": f"weekly_budget_rub must be >= target_cpa_rub * 20 ({planned_cpa_rub * 20})"},
                            400,
                        )
                        return

                    plan_changes.append(
                        {
                            "type": "update_weekly_budget",
                            "field": "WeeklySpendLimit",
                            "old_value": format_rub_value(old_value),
                            "new_value": format_rub_value(change_value),
                        }
                    )
                    summary_parts.append(
                        f"WeeklySpendLimit {format_rub_value(old_value)} -> {format_rub_value(change_value)}"
                    )
                    planned_weekly_budget_rub = change_value
                    update_payload["weekly_budget_rub"] = format_rub_value(change_value)

            plan = {
                "campaign_id": str(campaign_id),
                "target": "production",
                "changes": plan_changes,
                "summary": "; ".join(summary_parts),
                "apply_ready": True,
                "update_payload": update_payload,
            }

            state = deep_copy_json(RUNTIME_STATE)
            state["session_mode"] = "awaiting_confirm_update"
            state["last_plan"] = plan
            state["last_proposal"] = None
            save_runtime_state(state)

            self._send_json(
                {
                    "status": "success",
                    "plan": {
                        "campaign_id": plan["campaign_id"],
                        "changes": plan["changes"],
                        "summary": plan["summary"],
                        "apply_ready": plan["apply_ready"],
                    },
                    "session_mode": state["session_mode"],
                },
                200,
            )
            return

        if self.path == "/apply_campaign_update":
            confirm = resolve_confirm(data)
            if not confirm:
                self._send_json({"status": "error", "message": "apply_campaign_update requires explicit confirm=true"}, 400)
                return

            state = deep_copy_json(RUNTIME_STATE)
            if state.get("session_mode") != "awaiting_confirm_update":
                self._send_json({"status": "error", "message": "session_mode must be awaiting_confirm_update"}, 409)
                return

            pending_plan = state.get("last_plan")
            pending_proposal = state.get("last_proposal")

            pending_update = pending_plan if isinstance(pending_plan, dict) else pending_proposal
            pending_key = "last_plan" if isinstance(pending_plan, dict) else "last_proposal"

            if not isinstance(pending_update, dict):
                self._send_json({"status": "error", "message": "last_plan or last_proposal is not initialized"}, 409)
                return

            campaign_id = normalize_campaign_id(pending_update.get("campaign_id"))
            if campaign_id is None:
                self._send_json({"status": "error", "message": f"{pending_key} campaign_id is invalid"}, 409)
                return

            update_payload = pending_update.get("update_payload")
            if not isinstance(update_payload, dict):
                self._send_json({"status": "error", "message": f"{pending_key} update_payload is invalid"}, 409)
                return

            target_cpa_rub = normalize_positive_number(update_payload.get("target_cpa_rub"))
            weekly_budget_rub = normalize_positive_number(update_payload.get("weekly_budget_rub"))

            if target_cpa_rub is None and weekly_budget_rub is None:
                self._send_json({"status": "error", "message": f"{pending_key} has no supported update fields"}, 409)
                return

            client = YandexDirectClient.for_target("production")

            try:
                current_result = client.get_campaign_details(campaign_id)
            except YandexDirectClientError as e:
                self._send_json(
                    {"status": "error", "message": str(e), "target": "production", "campaign_id": str(campaign_id)},
                    502,
                )
                return

            campaigns = current_result.get("result", {}).get("Campaigns", [])
            if not campaigns:
                self._send_json(
                    {"status": "error", "message": "campaign not found", "target": "production", "campaign_id": str(campaign_id), "raw": current_result},
                    404,
                )
                return

            current_strategy = extract_current_upc_strategy(campaigns[0])
            current_goal_id = current_strategy["goal_id"]
            current_cpa_rub = micros_to_rub(current_strategy["cpa_micros"])
            current_weekly_budget_rub = micros_to_rub(current_strategy["weekly_budget_micros"])

            final_cpa_rub = target_cpa_rub if target_cpa_rub is not None else current_cpa_rub
            final_weekly_budget_rub = weekly_budget_rub if weekly_budget_rub is not None else current_weekly_budget_rub

            if final_weekly_budget_rub < final_cpa_rub * 20:
                self._send_json(
                    {"status": "error", "message": f"weekly_budget_rub must be >= target_cpa_rub * 20 ({final_cpa_rub * 20})"},
                    400,
                )
                return

            try:
                result = client.update_campaign_production(
                    campaign_id=campaign_id,
                    name=None,
                    goal_id=current_goal_id,
                    cpa_micros=rub_to_micros(final_cpa_rub),
                    weekly_budget_micros=rub_to_micros(final_weekly_budget_rub),
                )
            except YandexDirectClientError as e:
                self._send_json(
                    {"status": "error", "message": str(e), "target": "production", "campaign_id": str(campaign_id)},
                    502,
                )
                return

            parsed = parse_update_result(result, "campaign")
            if not parsed["ok"]:
                payload = parsed["payload"]
                payload["target"] = "production"
                payload["campaign_id"] = str(campaign_id)
                self._send_json(payload, parsed["status"])
                return

            state["session_mode"] = "analyze_campaign"
            state["last_plan"] = None
            state["last_proposal"] = None
            save_runtime_state(state)

            response_payload = {
                "status": "success",
                "campaign_id": parsed["payload"]["id"],
                "target": "production",
                "warnings": parsed["payload"]["warnings"],
                "updated_strategy": {
                    "target_cpa_rub": format_rub_value(final_cpa_rub),
                    "weekly_budget_rub": format_rub_value(final_weekly_budget_rub),
                    "metrica_goal_id": str(current_goal_id),
                },
                "result": result,
            }

            if pending_key == "last_plan":
                response_payload["applied_plan"] = {
                    "campaign_id": pending_update["campaign_id"],
                    "changes": pending_update.get("changes", []),
                    "summary": pending_update.get("summary"),
                    "apply_ready": pending_update.get("apply_ready", True),
                }
            else:
                response_payload["applied_proposal"] = {
                    "campaign_id": pending_update["campaign_id"],
                    "changes": pending_update.get("changes", []),
                    "summary": pending_update.get("summary"),
                }

            self._send_json(response_payload, 200)
            return

        if self.path == "/get_campaign_stats":
            raw_target = data.get("target", "production")
            target = raw_target.strip().lower() if isinstance(raw_target, str) else ""

            if target != "production":
                self._send_json(
                    {
                        "status": "error",
                        "message": "Reports API step 1 is enabled only for production; sandbox support is not confirmed",
                    },
                    400,
                )
                return

            campaign_id = normalize_campaign_id(data.get("campaign_id"))
            if campaign_id is None:
                self._send_json({"status": "error", "message": "campaign_id must be a positive integer or numeric string"}, 400)
                return

            payload, status_code = build_campaign_stats_response(
                campaign_id=campaign_id,
                raw_date_from=data.get("date_from"),
                raw_date_to=data.get("date_to"),
            )
            self._send_json(payload, status_code)
            return


        if self.path == "/get_search_terms":
            raw_target = data.get("target", "production")
            target = raw_target.strip().lower() if isinstance(raw_target, str) else ""

            if target != "production":
                self._send_json(
                    {
                        "status": "error",
                        "message": "Reports API search terms are enabled only for production; sandbox support is not confirmed",
                    },
                    400,
                )
                return

            campaign_id = normalize_campaign_id(data.get("campaign_id"))
            if campaign_id is None:
                self._send_json({"status": "error", "message": "campaign_id must be a positive integer or numeric string"}, 400)
                return

            analyze = data.get("analyze", False)
            if not isinstance(analyze, bool):
                self._send_json({"status": "error", "message": "analyze must be boolean when provided"}, 400)
                return

            payload, status_code = build_search_terms_response(
                campaign_id=campaign_id,
                raw_date_from=data.get("date_from"),
                raw_date_to=data.get("date_to"),
                analyze=analyze,
            )
            self._send_json(payload, status_code)
            return


        if self.path == "/preview_search_term_negatives":
            raw_target = data.get("target", "production")
            target = raw_target.strip().lower() if isinstance(raw_target, str) else ""

            if target != "production":
                self._send_json(
                    {
                        "status": "error",
                        "message": "Search term negative preview is enabled only for production; sandbox support is not confirmed",
                    },
                    400,
                )
                return

            campaign_id = normalize_campaign_id(data.get("campaign_id"))
            if campaign_id is None:
                self._send_json({"status": "error", "message": "campaign_id must be a positive integer or numeric string"}, 400)
                return

            payload, status_code = build_search_term_negative_preview_response(
                campaign_id=campaign_id,
                raw_date_from=data.get("date_from"),
                raw_date_to=data.get("date_to"),
                raw_target_cpa=data.get("target_cpa"),
            )
            self._send_json(payload, status_code)
            return


        if self.path == "/apply_search_term_negatives":
            confirm = resolve_confirm(data)
            if not confirm:
                self._send_json({"status": "error", "message": "apply_search_term_negatives requires explicit confirm=true"}, 400)
                return

            raw_target = data.get("target", "production")
            target = raw_target.strip().lower() if isinstance(raw_target, str) else ""

            if target != "production":
                self._send_json(
                    {
                        "status": "error",
                        "message": "Search term negative apply is enabled only for production; sandbox support is not confirmed",
                    },
                    400,
                )
                return

            campaign_id = normalize_campaign_id(data.get("campaign_id"))
            if campaign_id is None:
                self._send_json({"status": "error", "message": "campaign_id must be a positive integer or numeric string"}, 400)
                return

            payload, status_code = build_apply_search_term_negatives_response(
                campaign_id=campaign_id,
                raw_date_from=data.get("date_from"),
                raw_date_to=data.get("date_to"),
                raw_target_cpa=data.get("target_cpa"),
            )
            self._send_json(payload, status_code)
            return

        if self.path == "/analyze_campaign":
            campaign_id = normalize_campaign_id(data.get("campaign_id"))
            if campaign_id is None:
                self._send_json({"status": "error", "message": "campaign_id must be a positive integer or numeric string"}, 400)
                return

            focus = data.get("focus")
            if focus is not None:
                if not isinstance(focus, str) or not focus.strip():
                    self._send_json({"status": "error", "message": "focus must be a non-empty string when provided"}, 400)
                    return
                focus = focus.strip().lower()

            stats_payload, stats_status_code = build_campaign_stats_response(
                campaign_id=campaign_id,
                raw_date_from=data.get("date_from"),
                raw_date_to=data.get("date_to"),
            )

            if stats_payload.get("status") == "error":
                self._send_json(stats_payload, stats_status_code)
                return

            analysis_payload = build_campaign_analysis_response(stats_payload, focus=focus)
            self._send_json(analysis_payload, stats_status_code)
            return

        if self.path == "/get_campaign_status":
            target = resolve_target(data)
            if target not in ALLOWED_TARGETS:
                self._send_json({"status": "error", "message": "target must be 'sandbox' or 'production'"}, 400)
                return

            campaign_id = normalize_campaign_id(data.get("campaign_id"))
            if campaign_id is None:
                self._send_json({"status": "error", "message": "campaign_id must be a positive integer or numeric string"}, 400)
                return

            client = YandexDirectClient.for_target(target)

            try:
                result = client.get_campaign_details(campaign_id)
            except YandexDirectClientError as e:
                self._send_json(
                    {"status": "error", "message": str(e), "target": target, "campaign_id": str(campaign_id)},
                    502,
                )
                return

            campaigns = result.get("result", {}).get("Campaigns", [])
            if not campaigns:
                self._send_json(
                    {"status": "error", "message": "campaign not found", "target": target, "campaign_id": str(campaign_id), "raw": result},
                    404,
                )
                return

            campaign = campaigns[0]

            self._send_json(
                {"status": "success", "target": target, "campaign_id": str(campaign_id), "campaign": campaign},
                200,
            )
            return

        if self.path == "/create_ad_group":
            target = resolve_target(data)
            if target not in ALLOWED_TARGETS:
                self._send_json({"status": "error", "message": "target must be 'sandbox' or 'production'"}, 400)
                return

            campaign_id = normalize_campaign_id(data.get("campaign_id"))
            if campaign_id is None:
                self._send_json({"status": "error", "message": "campaign_id must be a positive integer or numeric string"}, 400)
                return

            ad_group_name = data.get("ad_group_name")
            if not isinstance(ad_group_name, str) or not ad_group_name.strip():
                self._send_json({"status": "error", "message": "ad_group_name must be a non-empty string"}, 400)
                return

            region_ids = normalize_region_ids(data.get("region_ids"))
            if region_ids is None:
                self._send_json(
                    {"status": "error", "message": "region_ids must be a non-empty array of integers or numeric strings"},
                    400,
                )
                return

            offer_retargeting = resolve_offer_retargeting(data)
            if offer_retargeting not in ALLOWED_OFFER_RETARGETING:
                self._send_json({"status": "error", "message": "offer_retargeting must be 'YES' or 'NO'"}, 400)
                return

            negative_keywords = normalize_negative_keywords(data.get("negative_keywords"))
            if data.get("negative_keywords") is not None and negative_keywords is None:
                self._send_json({"status": "error", "message": "negative_keywords must be an array of non-empty strings"}, 400)
                return

            confirm = resolve_confirm(data)
            if target == "production" and not confirm:
                self._send_json(
                    {"status": "error", "message": "production ad group create requires explicit confirm=true", "target": "production"},
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
                    {"status": "error", "message": str(e), "target": target, "campaign_id": str(campaign_id)},
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
                self._send_json({"status": "error", "message": "target must be 'sandbox' or 'production'"}, 400)
                return

            ad_group_id = normalize_ad_group_id(data.get("ad_group_id"))
            if ad_group_id is None:
                self._send_json({"status": "error", "message": "ad_group_id must be a positive integer or numeric string"}, 400)
                return

            client = YandexDirectClient.for_target(target)

            try:
                result = client.get_ad_group_details(ad_group_id)
            except YandexDirectClientError as e:
                self._send_json(
                    {"status": "error", "message": str(e), "target": target, "ad_group_id": str(ad_group_id)},
                    502,
                )
                return

            ad_groups = result.get("result", {}).get("AdGroups", [])
            if not ad_groups:
                self._send_json(
                    {"status": "error", "message": "ad_group not found", "target": target, "ad_group_id": str(ad_group_id), "raw": result},
                    404,
                )
                return

            ad_group = ad_groups[0]

            self._send_json(
                {"status": "success", "target": target, "ad_group_id": str(ad_group_id), "ad_group": ad_group},
                200,
            )
            return

        if self.path == "/list_ad_groups":
            target = resolve_target(data)
            if target not in ALLOWED_TARGETS:
                self._send_json({"status": "error", "message": "target must be 'sandbox' or 'production'"}, 400)
                return

            campaign_id = normalize_campaign_id(data.get("campaign_id"))
            if campaign_id is None:
                self._send_json({"status": "error", "message": "campaign_id must be a positive integer or numeric string"}, 400)
                return

            client = YandexDirectClient.for_target(target)

            try:
                campaign_result = client.get_campaign_details(campaign_id)
            except YandexDirectClientError as e:
                self._send_json(
                    {"status": "error", "message": str(e), "target": target, "campaign_id": str(campaign_id)},
                    502,
                )
                return

            campaigns = campaign_result.get("result", {}).get("Campaigns", [])
            if not campaigns:
                self._send_json(
                    {
                        "status": "error",
                        "message": "campaign not found",
                        "target": target,
                        "campaign_id": str(campaign_id),
                        "raw": campaign_result,
                    },
                    404,
                )
                return

            try:
                result = client.list_ad_groups(campaign_id)
            except YandexDirectClientError as e:
                self._send_json(
                    {"status": "error", "message": str(e), "target": target, "campaign_id": str(campaign_id)},
                    502,
                )
                return

            raw_ad_groups = result.get("result", {}).get("AdGroups", [])
            ad_groups = []

            for ad_group in raw_ad_groups:
                negative_keywords = []
                raw_negative_keywords = ad_group.get("NegativeKeywords")
                if isinstance(raw_negative_keywords, dict):
                    negative_keywords = raw_negative_keywords.get("Items", []) or []
                ad_groups.append(
                    {
                        "id": str(ad_group.get("Id")),
                        "name": ad_group.get("Name"),
                        "campaign_id": str(ad_group.get("CampaignId")),
                        "region_ids": ad_group.get("RegionIds", []),
                        "negative_keywords": negative_keywords,
                        "status": ad_group.get("Status"),
                        "serving_status": ad_group.get("ServingStatus"),
                        "type": ad_group.get("Type"),
                    }
                )

            self._send_json(
                {
                    "status": "success",
                    "target": target,
                    "campaign_id": str(campaign_id),
                    "ad_groups": ad_groups,
                },
                200,
            )
            return

        if self.path == "/save_approved_pattern":
            pattern_type = data.get("pattern_type")
            if pattern_type not in {
                "negative_keywords_defaults",
                "sitelinks_defaults",
                "autotargeting_defaults",
                "campaign_name_suffix",
                "budget_defaults",
            }:
                self._send_json({"status": "error", "message": "unsupported pattern_type"}, 400)
                return

            value = data.get("value")
            normalized_value = None

            if pattern_type == "negative_keywords_defaults":
                normalized_value = normalize_negative_keywords(value)
                if normalized_value is None or not normalized_value:
                    self._send_json({"status": "error", "message": "value must be a non-empty array of strings"}, 400)
                    return
            elif pattern_type == "sitelinks_defaults":
                normalized_value = normalize_sitelinks_defaults(value)
                if normalized_value is None:
                    self._send_json(
                        {"status": "error", "message": "value must be a non-empty sitelinks array under https://artfarfor.com"},
                        400,
                    )
                    return
            elif pattern_type == "autotargeting_defaults":
                normalized_value = normalize_autotargeting_settings(value)
                if normalized_value is None:
                    self._send_json(
                        {"status": "error", "message": "value must be a confirmed autotargeting settings object"},
                        400,
                    )
                    return
            elif pattern_type == "campaign_name_suffix":
                if not isinstance(value, str) or not value.strip():
                    self._send_json({"status": "error", "message": "value must be a non-empty string"}, 400)
                    return
                normalized_value = value
            elif pattern_type == "budget_defaults":
                normalized_value = normalize_budget_defaults(value)
                if normalized_value is None:
                    self._send_json(
                        {
                            "status": "error",
                            "message": "value must be a budget_defaults object compatible with current validate rules",
                        },
                        400,
                    )
                    return

            state = deep_copy_json(RUNTIME_STATE)
            approved_patterns = state.get("approved_patterns")
            if not isinstance(approved_patterns, dict):
                approved_patterns = build_default_approved_patterns()

            approved_patterns[pattern_type] = deep_copy_json(normalized_value)
            state["approved_patterns"] = approved_patterns
            state["draft_meta"]["last_action"] = "save_approved_pattern"
            save_runtime_state(state)

            self._send_json(
                {
                    "status": "success",
                    "approved_patterns": state["approved_patterns"],
                },
                200,
            )
            return

        if self.path == "/get_approved_patterns":
            state = deep_copy_json(RUNTIME_STATE)
            approved_patterns = state.get("approved_patterns")
            if not isinstance(approved_patterns, dict):
                approved_patterns = build_default_approved_patterns()

            self._send_json(
                {
                    "status": "success",
                    "approved_patterns": approved_patterns,
                },
                200,
            )
            return

        if self.path == "/save_creative_spec":
            creative_spec = data.get("creative_spec")
            if not isinstance(creative_spec, dict) or not creative_spec:
                self._send_json({"status": "error", "message": "creative_spec must be a non-empty object"}, 400)
                return

            state = deep_copy_json(RUNTIME_STATE)
            state["creative_spec"] = deep_copy_json(creative_spec)
            state["draft_meta"]["last_action"] = "save_creative_spec"
            save_runtime_state(state)

            self._send_json(
                {
                    "status": "success",
                    "creative_spec": state["creative_spec"],
                },
                200,
            )
            return

        if self.path == "/get_creative_spec":
            state = deep_copy_json(RUNTIME_STATE)
            self._send_json(
                {
                    "status": "success",
                    "creative_spec": state.get("creative_spec"),
                },
                200,
            )
            return

        if self.path == "/get_supa_status":
            self._send_json(
                {
                    "status": "success",
                    "supa": {
                        "configured": settings.is_supa_configured,
                        "base_url": settings.supa_base_url,
                        "has_api_key": bool(settings.supa_api_key.strip()),
                        "project_id": settings.supa_project_id,
                    },
                },
                200,
            )
            return

        if self.path == "/upload_image_to_supa":
            if not settings.is_supa_configured or not settings.supa_upload_url:
                self._send_json({"status": "error", "message": "Supa is not configured"}, 400)
                return

            image_base64 = normalize_non_empty_string(data.get("image_base64")) or normalize_non_empty_string(data.get("image_data_base64"))
            if image_base64 is None:
                self._send_json({"status": "error", "message": "image_base64 must be a non-empty string"}, 400)
                return

            filename = normalize_non_empty_string(data.get("filename")) or normalize_non_empty_string(data.get("name"))
            if filename is None:
                self._send_json({"status": "error", "message": "filename must be a non-empty string"}, 400)
                return

            content_type = normalize_non_empty_string(data.get("content_type"))

            try:
                image_bytes = base64.b64decode(image_base64, validate=True)
            except (binascii.Error, ValueError):
                self._send_json({"status": "error", "message": "image_base64 must be valid base64"}, 400)
                return

            result = upload_image_to_supa_api(
                filename=filename,
                image_bytes=image_bytes,
                content_type=content_type,
            )

            if not result["ok"]:
                self._send_json(
                    {
                        "status": "error",
                        "message": "Supa upload failed",
                        "raw": result["payload"],
                    },
                    result["status_code"] if isinstance(result["status_code"], int) else 502,
                )
                return

            payload = result["payload"]
            upload_url = payload.get("url") if isinstance(payload, dict) else None
            if payload.get("result") != "success" or not isinstance(upload_url, str) or not upload_url.strip():
                self._send_json(
                    {
                        "status": "error",
                        "message": "unexpected Supa upload response",
                        "raw": payload,
                    },
                    502,
                )
                return

            state = deep_copy_json(RUNTIME_STATE)
            if not isinstance(state.get("media_library"), list):
                state["media_library"] = []
            state["render_result_url"] = upload_url.strip()
            state["media_library"].append(
                {
                    "type": "image",
                    "source": "supa_upload",
                    "url": state["render_result_url"],
                }
            )
            state["draft_meta"]["last_action"] = "upload_image_to_supa"
            save_runtime_state(state)

            self._send_json(
                {
                    "status": "success",
                    "supa_upload": payload,
                    "filename": filename,
                    "stored_in_state": True,
                },
                200,
            )
            return

        if self.path == "/get_media_state":
            state = deep_copy_json(RUNTIME_STATE)
            self._send_json(
                {
                    "status": "success",
                    "media_state": extract_media_state(state),
                },
                200,
            )
            return

        if self.path == "/get_media_library":
            state = deep_copy_json(RUNTIME_STATE)
            self._send_json(
                {
                    "status": "success",
                    "media_library": extract_media_state(state)["media_library"],
                },
                200,
            )
            return

        if self.path == "/get_draft_media":
            state = deep_copy_json(RUNTIME_STATE)
            self._send_json(
                {
                    "status": "success",
                    "draft_media": extract_media_state(state)["draft_media"],
                },
                200,
            )
            return

        if self.path == "/find_site_images_for_theme":
            theme = normalize_non_empty_string(data.get("theme"))
            if theme is None:
                self._send_json({"status": "error", "message": "theme must be a non-empty string"}, 400)
                return

            limit = normalize_non_negative_int(data.get("limit"))
            if limit is None or limit == 0:
                limit = DEFAULT_SITE_IMAGE_LIMIT

            discovery = find_site_images_for_theme_internal(theme=theme, limit=min(limit, 10))
            self._send_json(discovery["payload"], discovery["status"])
            return

        if self.path == "/apply_site_image_to_draft_ad":
            theme = normalize_non_empty_string(data.get("theme"))
            if theme is None:
                self._send_json({"status": "error", "message": "theme must be a non-empty string"}, 400)
                return

            scope = data.get("scope")
            if scope not in {"campaign", "ad_group"}:
                self._send_json({"status": "error", "message": "scope must be 'campaign' or 'ad_group'"}, 400)
                return

            ad_index = normalize_non_negative_int(data.get("ad_index"))
            if ad_index is None:
                self._send_json({"status": "error", "message": "ad_index must be a non-negative integer or numeric string"}, 400)
                return

            ad_group_index = None
            if scope == "ad_group":
                ad_group_index = normalize_non_negative_int(data.get("ad_group_index"))
                if ad_group_index is None:
                    self._send_json({"status": "error", "message": "ad_group_index must be a non-negative integer or numeric string for scope=ad_group"}, 400)
                    return

            current_state = deep_copy_json(RUNTIME_STATE)
            current_draft_campaign = current_state.get("draft_campaign")
            if not isinstance(current_draft_campaign, dict):
                self._send_json({"status": "error", "message": "draft_campaign is not initialized"}, 409)
                return
            existing_draft_ad, _ = resolve_draft_ad_reference(
                draft_campaign=current_draft_campaign,
                scope=scope,
                ad_index=ad_index,
                ad_group_index=ad_group_index,
            )
            if existing_draft_ad is None:
                self._send_json({"status": "error", "message": "draft ad not found for provided scope/indexes"}, 400)
                return

            target = resolve_target({"target": data.get("target", "production")})
            if target not in ALLOWED_TARGETS:
                self._send_json({"status": "error", "message": "target must be 'sandbox' or 'production'"}, 400)
                return

            confirm = resolve_confirm(data)
            if target == "production" and not confirm:
                self._send_json(
                    {"status": "error", "message": "production site image apply requires explicit confirm=true", "target": "production"},
                    400,
                )
                return

            match_index = normalize_non_negative_int(data.get("match_index"))
            if match_index is None:
                match_index = 0

            requested_limit = normalize_non_negative_int(data.get("limit"))
            if "limit" in data:
                if requested_limit is None or requested_limit < 1:
                    self._send_json({"status": "error", "message": "limit must be a positive integer or numeric string when provided"}, 400)
                    return
            else:
                requested_limit = 1

            discovery = find_site_images_for_theme_internal(
                theme=theme,
                limit=max(match_index + requested_limit, DEFAULT_SITE_IMAGE_LIMIT),
            )
            if not discovery["ok"]:
                self._send_json(discovery["payload"], discovery["status"])
                return

            matches = discovery["payload"].get("matches", [])
            if not matches:
                self._send_json(
                    {"status": "no_match", "theme": theme, "message": "no verified site image found for theme", "matches": []},
                    200,
                )
                return

            if match_index >= len(matches):
                self._send_json({"status": "error", "message": "match_index is out of range"}, 400)
                return

            selected_matches = matches[match_index : match_index + requested_limit]
            uploaded_hashes = []
            for selected_match in selected_matches:
                download_result = download_site_image_bytes(selected_match.get("image_url", ""))
                if not download_result["ok"]:
                    self._send_json(download_result["payload"], download_result["status"])
                    return

                encoded_image = base64.b64encode(download_result["payload"]["image_bytes"]).decode("ascii")
                filename = download_result["payload"]["filename"]
                upload_result = upload_ad_image_via_direct(
                    target=target,
                    name=filename,
                    image_data_base64=encoded_image,
                )
                if not upload_result["ok"]:
                    self._send_json(upload_result["payload"], upload_result["status"])
                    return
                uploaded_hashes.append(upload_result["payload"]["ad_image_hash"])

            state = deep_copy_json(RUNTIME_STATE)
            if not isinstance(state.get("media_library"), list):
                state["media_library"] = []

            for selected_match, uploaded_hash in zip(selected_matches, uploaded_hashes):
                add_yandex_uploaded_image_to_state(state, uploaded_hash)
                state["media_library"].append(
                    {
                        "type": "image",
                        "source": "artfarfor_site",
                        "theme": theme,
                        "page_url": selected_match["page_url"],
                        "image_url": selected_match["image_url"],
                    }
                )

            linked = link_image_hashes_to_draft_state(
                state=state,
                scope=scope,
                ad_index=ad_index,
                ad_group_index=ad_group_index,
                ad_image_hashes=uploaded_hashes,
            )
            if not linked["ok"]:
                self._send_json(linked["payload"], linked["status"])
                return

            state = linked["payload"]["state"]
            state["draft_meta"]["last_action"] = "apply_site_image_to_draft_ad"
            save_runtime_state(state)

            self._send_json(
                {
                    "status": "success",
                    "theme": theme,
                    "selected_match": selected_matches[0],
                    "selected_matches": selected_matches,
                    "ad_image_hash": uploaded_hashes[0],
                    "ad_image_hashes": uploaded_hashes,
                    "draft_fragment": linked["payload"]["draft_fragment"],
                },
                200,
            )
            return

        if self.path == "/apply_site_images_to_all_draft_ads":
            theme = normalize_non_empty_string(data.get("theme"))
            if theme is None:
                self._send_json({"status": "error", "message": "theme must be a non-empty string"}, 400)
                return

            images_per_ad = normalize_non_negative_int(data.get("images_per_ad", 1))
            if images_per_ad is None or images_per_ad <= 0:
                self._send_json({"status": "error", "message": "images_per_ad must be a positive integer"}, 400)
                return

            state = deep_copy_json(RUNTIME_STATE)
            draft_campaign = state.get("draft_campaign")
            if not isinstance(draft_campaign, dict):
                self._send_json({"status": "error", "message": "draft_campaign is not initialized"}, 409)
                return

            normalize_draft_campaign_images(draft_campaign)
            ad_references = enumerate_draft_ad_references(draft_campaign)
            if not ad_references:
                self._send_json({"status": "error", "message": "draft_campaign does not contain any ads"}, 400)
                return

            discovery = find_site_images_for_theme_internal(theme=theme, limit=min(images_per_ad, 10))
            if not discovery["ok"]:
                self._send_json(discovery["payload"], discovery["status"])
                return

            selected_matches = discovery["payload"].get("matches", [])
            uploaded_items = []
            updated_ads = []
            warnings = []

            if not selected_matches:
                warnings.append(
                    {
                        "message": "no verified site images found for theme",
                        "theme": theme,
                        "affected_ads": len(ad_references),
                    }
                )
            else:
                for selected_match in selected_matches:
                    download_result = download_site_image_bytes(selected_match.get("image_url", ""))
                    if not download_result["ok"]:
                        warnings.append(
                            {
                                "message": "failed to download verified site image",
                                "image_url": selected_match.get("image_url"),
                                "raw": download_result["payload"],
                            }
                        )
                        continue

                    encoded_image = base64.b64encode(download_result["payload"]["image_bytes"]).decode("ascii")
                    filename = download_result["payload"]["filename"]
                    upload_result = upload_ad_image_via_direct(
                        target="production",
                        name=filename,
                        image_data_base64=encoded_image,
                    )
                    if not upload_result["ok"]:
                        warnings.append(
                            {
                                "message": "failed to upload verified site image to Yandex Direct",
                                "image_url": selected_match.get("image_url"),
                                "raw": upload_result["payload"],
                            }
                        )
                        continue

                    uploaded_items.append(
                        {
                            "match": selected_match,
                            "ad_image_hash": upload_result["payload"]["ad_image_hash"],
                        }
                    )

            if not uploaded_items:
                state["draft_meta"]["last_action"] = "apply_site_images_to_all_draft_ads"
                save_runtime_state(state)
                self._send_json(
                    {
                        "status": "success",
                        "theme": theme,
                        "updated_ads": [],
                        "warnings": warnings,
                    },
                    200,
                )
                return

            if not isinstance(state.get("media_library"), list):
                state["media_library"] = []

            uploaded_hashes = []
            for uploaded_item in uploaded_items:
                uploaded_hash = uploaded_item["ad_image_hash"]
                uploaded_hashes.append(uploaded_hash)
                add_yandex_uploaded_image_to_state(state, uploaded_hash)
                state["media_library"].append(
                    {
                        "type": "image",
                        "source": "artfarfor_site",
                        "theme": theme,
                        "page_url": uploaded_item["match"]["page_url"],
                        "image_url": uploaded_item["match"]["image_url"],
                    }
                )

            if len(uploaded_hashes) < images_per_ad:
                warnings.append(
                    {
                        "message": "fewer verified theme images were uploaded than requested",
                        "requested": images_per_ad,
                        "uploaded": len(uploaded_hashes),
                    }
                )

            for ad_reference in ad_references:
                linked = link_image_hashes_to_draft_state(
                    state=state,
                    scope=ad_reference["scope"],
                    ad_index=ad_reference["ad_index"],
                    ad_group_index=ad_reference.get("ad_group_index"),
                    ad_image_hashes=uploaded_hashes,
                )
                if not linked["ok"]:
                    warnings.append(
                        {
                            "message": "failed to link uploaded images to draft ad",
                            "scope": ad_reference["scope"],
                            "ad_index": ad_reference["ad_index"],
                            **({} if "ad_group_index" not in ad_reference else {"ad_group_index": ad_reference["ad_group_index"]}),
                            "raw": linked["payload"],
                        }
                    )
                    continue

                state = linked["payload"]["state"]
                draft_fragment = linked["payload"]["draft_fragment"]
                updated_ads.append(
                    {
                        "scope": ad_reference["scope"],
                        "ad_index": ad_reference["ad_index"],
                        **({} if "ad_group_index" not in ad_reference else {"ad_group_index": ad_reference["ad_group_index"]}),
                        "ad_image_hash": draft_fragment.get("ad_image_hash"),
                        "ad_image_hashes": draft_fragment.get("ad_image_hashes", []),
                    }
                )

            state["draft_meta"]["last_action"] = "apply_site_images_to_all_draft_ads"
            save_runtime_state(state)

            self._send_json(
                {
                    "status": "success",
                    "theme": theme,
                    "updated_ads": updated_ads,
                    "warnings": warnings,
                },
                200,
            )
            return

        if self.path == "/draft_campaign_from_theme":
            theme = data.get("theme")
            if not isinstance(theme, str) or not theme.strip():
                self._send_json({"status": "error", "message": "theme must be a non-empty string"}, 400)
                return

            state = build_default_runtime_state()
            current_state = deep_copy_json(RUNTIME_STATE)
            approved_patterns = current_state.get("approved_patterns")
            if not isinstance(approved_patterns, dict):
                approved_patterns = build_default_approved_patterns()
            state["session_mode"] = "review_draft"
            state["approved_patterns"] = deep_copy_json(approved_patterns)
            if isinstance(current_state.get("media_library"), list):
                state["media_library"] = deep_copy_json(current_state["media_library"])
            state["draft_media"] = []
            state["last_uploaded_image_hash"] = current_state.get("last_uploaded_image_hash")
            state["creative_spec"] = deep_copy_json(current_state.get("creative_spec"))
            state["render_task"] = deep_copy_json(current_state.get("render_task"))
            state["render_result_url"] = current_state.get("render_result_url")
            state["draft_campaign"] = apply_approved_patterns_to_draft(
                theme.strip(),
                build_draft_campaign_from_theme(theme.strip()),
                state["approved_patterns"],
            )
            state["campaign_payload"] = None
            state["validation_result"] = None
            state["created_campaign_id"] = None
            state["draft_meta"]["theme"] = theme.strip()
            state["draft_meta"]["last_action"] = "draft_campaign_from_theme"
            state["draft_meta"]["revision_count"] = 0

            save_runtime_state(state)

            self._send_json(
                {
                    "status": "success",
                    "session_mode": state["session_mode"],
                    "draft_campaign": state["draft_campaign"],
                },
                200,
            )
            return

        if self.path == "/rebuild_draft_campaign":
            theme = data.get("theme")
            if not isinstance(theme, str) or not theme.strip():
                self._send_json({"status": "error", "message": "theme must be a non-empty string"}, 400)
                return

            ad_groups_count = normalize_non_negative_int(data.get("ad_groups_count", 4))
            if ad_groups_count is None or ad_groups_count <= 0:
                self._send_json({"status": "error", "message": "ad_groups_count must be a positive integer"}, 400)
                return

            ads_per_group = normalize_non_negative_int(data.get("ads_per_group", 4))
            if ads_per_group is None or ads_per_group <= 0:
                self._send_json({"status": "error", "message": "ads_per_group must be a positive integer"}, 400)
                return

            state = build_default_runtime_state()
            current_state = deep_copy_json(RUNTIME_STATE)
            approved_patterns = current_state.get("approved_patterns")
            if not isinstance(approved_patterns, dict):
                approved_patterns = build_default_approved_patterns()
            state["session_mode"] = "review_draft"
            state["approved_patterns"] = deep_copy_json(approved_patterns)
            if isinstance(current_state.get("media_library"), list):
                state["media_library"] = deep_copy_json(current_state["media_library"])
            state["draft_media"] = []
            state["last_uploaded_image_hash"] = current_state.get("last_uploaded_image_hash")
            state["creative_spec"] = deep_copy_json(current_state.get("creative_spec"))
            state["render_task"] = deep_copy_json(current_state.get("render_task"))
            state["render_result_url"] = current_state.get("render_result_url")
            state["draft_campaign"] = apply_approved_patterns_to_draft(
                theme.strip(),
                build_rebuild_draft_campaign(
                    theme=theme.strip(),
                    ad_groups_count=ad_groups_count,
                    ads_per_group=ads_per_group,
                ),
                state["approved_patterns"],
            )
            state["campaign_payload"] = None
            state["validation_result"] = None
            state["created_campaign_id"] = None
            state["draft_meta"]["theme"] = theme.strip()
            state["draft_meta"]["last_action"] = "rebuild_draft_campaign"
            state["draft_meta"]["revision_count"] = 0

            save_runtime_state(state)

            self._send_json(
                {
                    "status": "success",
                    "session_mode": state["session_mode"],
                    "draft_campaign": state["draft_campaign"],
                },
                200,
            )
            return

        if self.path == "/link_image_to_draft_ad":
            scope = data.get("scope")
            if scope not in {"campaign", "ad_group"}:
                self._send_json({"status": "error", "message": "scope must be 'campaign' or 'ad_group'"}, 400)
                return

            ad_index = normalize_non_negative_int(data.get("ad_index"))
            if ad_index is None:
                self._send_json({"status": "error", "message": "ad_index must be a non-negative integer or numeric string"}, 400)
                return

            ad_group_index = None
            if scope == "ad_group":
                ad_group_index = normalize_non_negative_int(data.get("ad_group_index"))
                if ad_group_index is None:
                    self._send_json({"status": "error", "message": "ad_group_index must be a non-negative integer or numeric string for scope=ad_group"}, 400)
                    return

            ad_image_hash = normalize_non_empty_string(data.get("ad_image_hash"))
            if ad_image_hash is None:
                self._send_json({"status": "error", "message": "ad_image_hash must be a non-empty string"}, 400)
                return

            state = deep_copy_json(RUNTIME_STATE)
            linked = link_image_hash_to_draft_state(
                state=state,
                scope=scope,
                ad_index=ad_index,
                ad_group_index=ad_group_index,
                ad_image_hash=ad_image_hash,
            )
            if not linked["ok"]:
                self._send_json(linked["payload"], linked["status"])
                return

            state = linked["payload"]["state"]
            state["draft_meta"]["last_action"] = "link_image_to_draft_ad"
            save_runtime_state(state)

            self._send_json(
                {
                    "status": "success",
                    "draft_fragment": linked["payload"]["draft_fragment"],
                    "draft_media": linked["payload"]["draft_media"],
                    "draft_campaign": linked["payload"]["draft_campaign"],
                },
                200,
            )
            return

        if self.path == "/link_multiple_images_to_draft_ad":
            scope = data.get("scope")
            if scope not in {"campaign", "ad_group"}:
                self._send_json({"status": "error", "message": "scope must be 'campaign' or 'ad_group'"}, 400)
                return

            ad_index = normalize_non_negative_int(data.get("ad_index"))
            if ad_index is None:
                self._send_json({"status": "error", "message": "ad_index must be a non-negative integer or numeric string"}, 400)
                return

            ad_group_index = None
            if scope == "ad_group":
                ad_group_index = normalize_non_negative_int(data.get("ad_group_index"))
                if ad_group_index is None:
                    self._send_json({"status": "error", "message": "ad_group_index must be a non-negative integer or numeric string for scope=ad_group"}, 400)
                    return

            raw_ad_image_hashes = data.get("ad_image_hashes")
            if not isinstance(raw_ad_image_hashes, list) or not raw_ad_image_hashes:
                self._send_json({"status": "error", "message": "ad_image_hashes must be a non-empty array of strings"}, 400)
                return

            normalized_hashes = normalize_ad_image_hashes(raw_ad_image_hashes)
            if not normalized_hashes:
                self._send_json({"status": "error", "message": "ad_image_hashes must contain at least one non-empty string"}, 400)
                return

            state = deep_copy_json(RUNTIME_STATE)
            linked = link_image_hashes_to_draft_state(
                state=state,
                scope=scope,
                ad_index=ad_index,
                ad_group_index=ad_group_index,
                ad_image_hashes=normalized_hashes,
            )
            if not linked["ok"]:
                self._send_json(linked["payload"], linked["status"])
                return

            state = linked["payload"]["state"]
            state["draft_meta"]["last_action"] = "link_multiple_images_to_draft_ad"
            save_runtime_state(state)

            self._send_json(
                {
                    "status": "success",
                    "draft_fragment": linked["payload"]["draft_fragment"],
                    "draft_media": linked["payload"]["draft_media"],
                    "draft_campaign": linked["payload"]["draft_campaign"],
                },
                200,
            )
            return

        if self.path == "/update_draft_ad":
            scope = data.get("scope")
            if scope not in {"campaign", "ad_group"}:
                self._send_json({"status": "error", "message": "scope must be 'campaign' or 'ad_group'"}, 400)
                return

            ad_index = normalize_non_negative_int(data.get("ad_index"))
            if ad_index is None:
                self._send_json({"status": "error", "message": "ad_index must be a non-negative integer or numeric string"}, 400)
                return

            ad_group_index = None
            if scope == "ad_group":
                ad_group_index = normalize_non_negative_int(data.get("ad_group_index"))
                if ad_group_index is None:
                    self._send_json({"status": "error", "message": "ad_group_index must be a non-negative integer or numeric string for scope=ad_group"}, 400)
                    return

            has_any_field = any(field in data for field in ("title", "text", "final_url"))
            if not has_any_field:
                self._send_json({"status": "error", "message": "at least one of title, text, final_url must be provided"}, 400)
                return

            updates = {}

            if "title" in data:
                title = normalize_non_empty_string(data.get("title"))
                if title is None:
                    self._send_json({"status": "error", "message": "title must be a non-empty string when provided"}, 400)
                    return
                updates["title"] = title

            if "text" in data:
                text = normalize_non_empty_string(data.get("text"))
                if text is None:
                    self._send_json({"status": "error", "message": "text must be a non-empty string when provided"}, 400)
                    return
                updates["text"] = text

            if "final_url" in data:
                final_url = normalize_draft_final_url(data.get("final_url"))
                if final_url is None:
                    self._send_json({"status": "error", "message": "final_url must be a non-empty https://artfarfor.com URL when provided"}, 400)
                    return
                updates["final_url"] = final_url

            state = deep_copy_json(RUNTIME_STATE)
            draft_campaign = state.get("draft_campaign")
            if not isinstance(draft_campaign, dict):
                self._send_json({"status": "error", "message": "draft_campaign is not initialized"}, 409)
                return

            normalize_draft_campaign_images(draft_campaign)
            draft_ad, _ = resolve_draft_ad_reference(
                draft_campaign=draft_campaign,
                scope=scope,
                ad_index=ad_index,
                ad_group_index=ad_group_index,
            )
            if draft_ad is None:
                self._send_json({"status": "error", "message": "draft ad not found for provided scope/indexes"}, 400)
                return

            if "title" in updates:
                draft_ad["title"] = updates["title"]
            if "text" in updates:
                draft_ad["text"] = updates["text"]
            if "final_url" in updates:
                draft_ad["final_url"] = updates["final_url"]

            state["draft_campaign"] = draft_campaign
            state["draft_meta"]["last_action"] = "update_draft_ad"
            state["draft_meta"]["revision_count"] = int(state["draft_meta"].get("revision_count", 0)) + 1
            save_runtime_state(state)

            self._send_json(
                {
                    "status": "success",
                    "draft_fragment": draft_ad,
                    "draft_campaign": state["draft_campaign"],
                },
                200,
            )
            return

        if self.path == "/get_draft_ad":
            scope = data.get("scope")
            if scope not in {"campaign", "ad_group"}:
                self._send_json({"status": "error", "message": "scope must be 'campaign' or 'ad_group'"}, 400)
                return

            ad_index = normalize_non_negative_int(data.get("ad_index"))
            if ad_index is None:
                self._send_json({"status": "error", "message": "ad_index must be a non-negative integer or numeric string"}, 400)
                return

            ad_group_index = None
            if scope == "ad_group":
                ad_group_index = normalize_non_negative_int(data.get("ad_group_index"))
                if ad_group_index is None:
                    self._send_json({"status": "error", "message": "ad_group_index must be a non-negative integer or numeric string for scope=ad_group"}, 400)
                    return

            state = deep_copy_json(RUNTIME_STATE)
            draft_campaign = state.get("draft_campaign")
            if not isinstance(draft_campaign, dict):
                self._send_json({"status": "error", "message": "draft_campaign is not initialized"}, 409)
                return

            normalize_draft_campaign_images(draft_campaign)
            draft_ad, _ = resolve_draft_ad_reference(
                draft_campaign=draft_campaign,
                scope=scope,
                ad_index=ad_index,
                ad_group_index=ad_group_index,
            )
            if draft_ad is None:
                self._send_json({"status": "error", "message": "draft ad not found for provided scope/indexes"}, 400)
                return

            self._send_json(
                {
                    "status": "success",
                    "draft_fragment": draft_ad,
                },
                200,
            )
            return

        if self.path == "/update_draft_ad_group":
            ad_group_index = normalize_non_negative_int(data.get("ad_group_index"))
            if ad_group_index is None:
                self._send_json({"status": "error", "message": "ad_group_index must be a non-negative integer or numeric string"}, 400)
                return

            group_name = normalize_non_empty_string(data.get("group_name"))
            if group_name is None:
                self._send_json({"status": "error", "message": "group_name must be a non-empty string"}, 400)
                return

            state = deep_copy_json(RUNTIME_STATE)
            draft_campaign = state.get("draft_campaign")
            if not isinstance(draft_campaign, dict):
                self._send_json({"status": "error", "message": "draft_campaign is not initialized"}, 409)
                return

            normalize_draft_campaign_images(draft_campaign)
            ad_group = resolve_draft_ad_group_reference(draft_campaign, ad_group_index)
            if ad_group is None:
                self._send_json({"status": "error", "message": "draft ad_group not found for provided ad_group_index"}, 400)
                return

            ad_group["group_name"] = group_name
            state["draft_campaign"] = draft_campaign
            state["draft_meta"]["last_action"] = "update_draft_ad_group"
            state["draft_meta"]["revision_count"] = int(state["draft_meta"].get("revision_count", 0)) + 1
            save_runtime_state(state)

            self._send_json(
                {
                    "status": "success",
                    "ad_group_fragment": ad_group,
                    "draft_campaign": state["draft_campaign"],
                },
                200,
            )
            return

        if self.path == "/get_draft_ad_group":
            ad_group_index = normalize_non_negative_int(data.get("ad_group_index"))
            if ad_group_index is None:
                self._send_json({"status": "error", "message": "ad_group_index must be a non-negative integer or numeric string"}, 400)
                return

            state = deep_copy_json(RUNTIME_STATE)
            draft_campaign = state.get("draft_campaign")
            if not isinstance(draft_campaign, dict):
                self._send_json({"status": "error", "message": "draft_campaign is not initialized"}, 409)
                return

            normalize_draft_campaign_images(draft_campaign)
            ad_group = resolve_draft_ad_group_reference(draft_campaign, ad_group_index)
            if ad_group is None:
                self._send_json({"status": "error", "message": "draft ad_group not found for provided ad_group_index"}, 400)
                return

            self._send_json(
                {
                    "status": "success",
                    "ad_group_fragment": ad_group,
                },
                200,
            )
            return

        if self.path == "/get_draft_campaign":
            state = deep_copy_json(RUNTIME_STATE)
            draft_campaign = state.get("draft_campaign")
            if not isinstance(draft_campaign, dict):
                self._send_json({"status": "error", "message": "draft_campaign is not initialized"}, 409)
                return

            normalize_draft_campaign_images(draft_campaign)
            self._send_json(
                {
                    "status": "success",
                    "session_mode": state.get("session_mode"),
                    "draft_campaign": draft_campaign,
                },
                200,
            )
            return

        if self.path == "/unlink_image_from_draft_ad":
            scope = data.get("scope")
            if scope not in {"campaign", "ad_group"}:
                self._send_json({"status": "error", "message": "scope must be 'campaign' or 'ad_group'"}, 400)
                return

            ad_index = normalize_non_negative_int(data.get("ad_index"))
            if ad_index is None:
                self._send_json({"status": "error", "message": "ad_index must be a non-negative integer or numeric string"}, 400)
                return

            ad_group_index = None
            if scope == "ad_group":
                ad_group_index = normalize_non_negative_int(data.get("ad_group_index"))
                if ad_group_index is None:
                    self._send_json({"status": "error", "message": "ad_group_index must be a non-negative integer or numeric string for scope=ad_group"}, 400)
                    return

            state = deep_copy_json(RUNTIME_STATE)
            draft_campaign = state.get("draft_campaign")
            if not isinstance(draft_campaign, dict):
                self._send_json({"status": "error", "message": "draft_campaign is not initialized"}, 409)
                return

            draft_ad, link_ref = resolve_draft_ad_reference(
                draft_campaign=draft_campaign,
                scope=scope,
                ad_index=ad_index,
                ad_group_index=ad_group_index,
            )
            if draft_ad is None or link_ref is None:
                self._send_json({"status": "error", "message": "draft ad not found for provided scope/indexes"}, 400)
                return

            normalize_draft_ad_images(draft_ad)
            draft_ad["ad_image_hash"] = None
            draft_ad["ad_image_hashes"] = []
            draft_ad["creative_spec"] = None

            raw_draft_media = state.get("draft_media")
            filtered_draft_media = []
            if isinstance(raw_draft_media, list):
                for item in raw_draft_media:
                    if not isinstance(item, dict):
                        filtered_draft_media.append(item)
                        continue
                    if item.get("scope") != link_ref["scope"]:
                        filtered_draft_media.append(item)
                        continue
                    if item.get("ad_index") != link_ref["ad_index"]:
                        filtered_draft_media.append(item)
                        continue
                    if link_ref["scope"] == "ad_group":
                        if item.get("ad_group_index") != link_ref.get("ad_group_index"):
                            filtered_draft_media.append(item)
                        continue
                    if item.get("ad_group_index") is not None:
                        filtered_draft_media.append(item)
                        continue

            state["draft_media"] = filtered_draft_media
            state["draft_campaign"] = draft_campaign
            state["draft_meta"]["last_action"] = "unlink_image_from_draft_ad"
            save_runtime_state(state)

            self._send_json(
                {
                    "status": "success",
                    "draft_fragment": draft_ad,
                    "draft_media": state["draft_media"],
                    "draft_campaign": state["draft_campaign"],
                },
                200,
            )
            return

        if self.path == "/clear_creative_spec":
            state = deep_copy_json(RUNTIME_STATE)
            state["creative_spec"] = None
            state["draft_meta"]["last_action"] = "clear_creative_spec"
            save_runtime_state(state)

            self._send_json(
                {
                    "status": "success",
                    "creative_spec": None,
                },
                200,
            )
            return

        if self.path == "/save_render_task":
            render_task = data.get("render_task")
            if not isinstance(render_task, dict) or not render_task:
                self._send_json({"status": "error", "message": "render_task must be a non-empty object"}, 400)
                return

            state = deep_copy_json(RUNTIME_STATE)
            state["render_task"] = deep_copy_json(render_task)
            state["draft_meta"]["last_action"] = "save_render_task"
            save_runtime_state(state)

            self._send_json(
                {
                    "status": "success",
                    "render_task": state["render_task"],
                },
                200,
            )
            return

        if self.path == "/get_render_task":
            state = deep_copy_json(RUNTIME_STATE)
            self._send_json(
                {
                    "status": "success",
                    "render_task": deep_copy_json(state.get("render_task")),
                },
                200,
            )
            return

        if self.path == "/save_render_result_url":
            render_result_url = normalize_non_empty_string(data.get("render_result_url"))
            if render_result_url is None:
                self._send_json({"status": "error", "message": "render_result_url must be a non-empty string"}, 400)
                return

            state = deep_copy_json(RUNTIME_STATE)
            state["render_result_url"] = render_result_url
            state["draft_meta"]["last_action"] = "save_render_result_url"
            save_runtime_state(state)

            self._send_json(
                {
                    "status": "success",
                    "render_result_url": state["render_result_url"],
                },
                200,
            )
            return

        if self.path == "/get_render_result_url":
            state = deep_copy_json(RUNTIME_STATE)
            self._send_json(
                {
                    "status": "success",
                    "render_result_url": state.get("render_result_url"),
                },
                200,
            )
            return

        if self.path == "/clear_render_state":
            state = deep_copy_json(RUNTIME_STATE)
            state["render_task"] = None
            state["render_result_url"] = None
            state["draft_meta"]["last_action"] = "clear_render_state"
            save_runtime_state(state)

            self._send_json(
                {
                    "status": "success",
                    "render_task": None,
                    "render_result_url": None,
                },
                200,
            )
            return

        if self.path == "/clear_draft_media":
            state = deep_copy_json(RUNTIME_STATE)
            state["draft_media"] = []
            state["draft_meta"]["last_action"] = "clear_draft_media"
            save_runtime_state(state)

            self._send_json(
                {
                    "status": "success",
                    "draft_media": [],
                },
                200,
            )
            return

        if self.path == "/revise_campaign_draft":
            instruction = data.get("instruction")
            if not isinstance(instruction, str) or not instruction.strip():
                self._send_json({"status": "error", "message": "instruction must be a non-empty string"}, 400)
                return

            state = deep_copy_json(RUNTIME_STATE)
            draft_campaign = state.get("draft_campaign")
            if not isinstance(draft_campaign, dict):
                self._send_json({"status": "error", "message": "draft_campaign is not initialized"}, 409)
                return

            raw_instruction = instruction.strip()
            lowered_instruction = raw_instruction.lower()
            supported = True

            if lowered_instruction.startswith("измени название кампании на "):
                new_name = raw_instruction[len("измени название кампании на "):].strip()
                if not new_name:
                    self._send_json({"status": "error", "message": "campaign name must be a non-empty string"}, 400)
                    return
                draft_campaign["campaign_name"] = new_name
            elif lowered_instruction.startswith("добавь группу "):
                group_name = raw_instruction[len("добавь группу "):].strip()
                if not group_name:
                    self._send_json({"status": "error", "message": "group name must be a non-empty string"}, 400)
                    return
                draft_campaign.setdefault("ad_groups", []).append(build_ad_group_draft(group_name))
            elif lowered_instruction.startswith("добавь минус-слово "):
                negative_keyword = raw_instruction[len("добавь минус-слово "):].strip()
                if not negative_keyword:
                    self._send_json({"status": "error", "message": "negative keyword must be a non-empty string"}, 400)
                    return
                draft_campaign.setdefault("negative_keywords", [])
                if negative_keyword not in draft_campaign["negative_keywords"]:
                    draft_campaign["negative_keywords"].append(negative_keyword)
            else:
                supported = False

            if not supported:
                self._send_json(
                    {
                        "status": "error",
                        "message": "instruction is not supported in v1 draft revision",
                    },
                    400,
                )
                return

            state["session_mode"] = "review_draft"
            state["draft_campaign"] = draft_campaign
            state["campaign_payload"] = None
            state["validation_result"] = None
            state["created_campaign_id"] = None
            state["draft_meta"]["last_action"] = "revise_campaign_draft"
            state["draft_meta"]["revision_count"] = int(state["draft_meta"].get("revision_count", 0)) + 1

            save_runtime_state(state)

            self._send_json(
                {
                    "status": "success",
                    "session_mode": state["session_mode"],
                    "draft_campaign": state["draft_campaign"],
                },
                200,
            )
            return

        if self.path == "/build_campaign_payload_from_draft":
            state = deep_copy_json(RUNTIME_STATE)
            draft_campaign = state.get("draft_campaign")
            if not isinstance(draft_campaign, dict):
                self._send_json({"status": "error", "message": "draft_campaign is not initialized"}, 409)
                return

            campaign_payload, missing_fields = build_campaign_payload_from_draft_state(draft_campaign)
            if missing_fields:
                self._send_json({"status": "error", "missing_fields": missing_fields}, 400)
                return

            state["session_mode"] = "awaiting_confirm_create"
            state["campaign_payload"] = campaign_payload
            state["validation_result"] = None
            state["created_campaign_id"] = None
            state["draft_meta"]["last_action"] = "build_campaign_payload_from_draft"

            save_runtime_state(state)

            self._send_json(
                {
                    "status": "success",
                    "session_mode": state["session_mode"],
                    "campaign_payload": campaign_payload,
                },
                200,
            )
            return

        if self.path == "/validate_draft_campaign":
            state = deep_copy_json(RUNTIME_STATE)
            draft_campaign = state.get("draft_campaign")
            if not isinstance(draft_campaign, dict):
                self._send_json({"status": "error", "message": "draft_campaign is not initialized"}, 409)
                return

            campaign_payload, missing_fields = build_campaign_payload_from_draft_state(draft_campaign)
            if missing_fields:
                self._send_json({"status": "error", "missing_fields": missing_fields}, 400)
                return

            is_valid, errors = validate_campaign(campaign_payload)
            state["campaign_payload"] = campaign_payload
            state["validation_result"] = {"valid": is_valid, "errors": errors}
            state["draft_meta"]["last_action"] = "validate_draft_campaign"
            if is_valid:
                state["session_mode"] = "awaiting_confirm_create"

            save_runtime_state(state)

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

        if self.path == "/create_campaign_from_draft":
            target = resolve_target(data)
            if target != "production":
                self._send_json({"status": "error", "message": "target must be 'production' for draft create flow"}, 400)
                return

            confirm = resolve_confirm(data)
            if not confirm:
                self._send_json(
                    {"status": "error", "message": "production draft create requires explicit confirm=true", "target": "production"},
                    400,
                )
                return

            state = deep_copy_json(RUNTIME_STATE)
            if state.get("session_mode") != "awaiting_confirm_create":
                self._send_json(
                    {"status": "error", "message": "session_mode must be awaiting_confirm_create before draft create"},
                    409,
                )
                return

            draft_campaign = state.get("draft_campaign")
            if not isinstance(draft_campaign, dict):
                self._send_json({"status": "error", "message": "draft_campaign is not initialized"}, 409)
                return

            campaign_payload, missing_fields = build_campaign_payload_from_draft_state(draft_campaign)
            if missing_fields:
                self._send_json({"status": "error", "missing_fields": missing_fields}, 400)
                return

            is_valid, errors = validate_campaign(campaign_payload)
            state["campaign_payload"] = campaign_payload
            state["validation_result"] = {"valid": is_valid, "errors": errors}
            if not is_valid:
                save_runtime_state(state)
                self._send_json({"status": "error", "valid": False, "errors": errors}, 400)
                return

            start_date = resolve_start_date(campaign_payload)
            client = YandexDirectClient.for_target("production")

            try:
                result = client.add_unified_campaign_production(
                    name=campaign_payload["campaign_name"],
                    start_date=start_date,
                    goal_id=int(campaign_payload["metrica_goal_id"]),
                    cpa_micros=rub_to_micros(campaign_payload["target_cpa_rub"]),
                    weekly_budget_micros=rub_to_micros(campaign_payload["weekly_budget_rub"]),
                    counter_id=DEFAULT_METRICA_COUNTER_ID,
                )
            except YandexDirectClientError as e:
                self._send_json({"status": "error", "message": str(e), "target": "production"}, 502)
                return

            parsed = parse_add_result(result, "campaign")
            if not parsed["ok"]:
                payload = parsed["payload"]
                payload["target"] = "production"
                self._send_json(payload, parsed["status"])
                return

            state["created_campaign_id"] = parsed["payload"]["id"]
            state["session_mode"] = "draft_campaign"
            state["draft_meta"]["last_action"] = "create_campaign_from_draft"
            save_runtime_state(state)

            self._send_json(
                {
                    "status": "success",
                    "campaign_id": parsed["payload"]["id"],
                    "target": "production",
                    "start_date": start_date,
                    "campaign_payload": campaign_payload,
                    "applied_defaults": {
                        "metrica_counter_id": DEFAULT_METRICA_COUNTER_ID,
                        "search_placement_types": dict(YandexDirectClient.DEFAULT_SEARCH_PLACEMENT_TYPES),
                        "network_placement_types": dict(YandexDirectClient.DEFAULT_NETWORK_PLACEMENT_TYPES),
                        "time_targeting_sent": False,
                    },
                },
                200,
            )
            return

        if self.path == "/apply_draft_to_production":
            confirm = resolve_confirm(data)
            if not confirm:
                self._send_json(
                    {"status": "error", "message": "apply_draft_to_production requires explicit confirm=true", "target": "production"},
                    400,
                )
                return

            campaign_id = normalize_campaign_id(data.get("campaign_id"))
            if campaign_id is None:
                self._send_json({"status": "error", "message": "campaign_id must be a positive integer or numeric string"}, 400)
                return

            state = deep_copy_json(RUNTIME_STATE)
            draft_campaign = state.get("draft_campaign")
            if not isinstance(draft_campaign, dict):
                self._send_json({"status": "error", "message": "draft_campaign is not initialized"}, 409)
                return

            normalize_draft_campaign_images(draft_campaign)
            client = YandexDirectClient.for_target("production")

            try:
                collected_entities = collect_campaign_entities_for_replace(client, campaign_id)
            except YandexDirectClientError as e:
                self._send_json(
                    {"status": "error", "message": str(e), "target": "production", "campaign_id": str(campaign_id)},
                    502,
                )
                return

            if not collected_entities["ok"]:
                self._send_json(collected_entities["payload"], collected_entities["status"])
                return

            collected_payload = collected_entities["payload"]
            cleanup = best_effort_cleanup_campaign_entities(
                client=client,
                ad_ids=collected_payload["ad_ids"],
                ad_group_ids=collected_payload["ad_group_ids"],
            )
            created = create_draft_structure_in_production_campaign(
                client=client,
                campaign_id=campaign_id,
                draft_campaign=draft_campaign,
                region_ids=collected_payload["region_ids"],
            )

            warnings = list(collected_payload.get("warnings", []))
            warnings.extend(cleanup.get("warnings", []))
            if created["errors"]:
                warnings.append("Some new ad groups or ads could not be created from the current draft.")

            if not created["created_any"]:
                self._send_json(
                    {
                        "status": "error",
                        "message": "draft apply did not create any new ad groups or ads",
                        "target": "production",
                        "campaign_id": str(campaign_id),
                        "cleanup": cleanup,
                        "created": {
                            "ad_group_ids": created["created_ad_group_ids"],
                            "ad_ids": created["created_ad_ids"],
                            "ad_groups": created["created_ad_groups"],
                            "ads": created["created_ads"],
                            "errors": created["errors"],
                            "region_ids": collected_payload["region_ids"],
                        },
                        "warnings": warnings,
                    },
                    400,
                )
                return

            state["draft_meta"]["last_action"] = "apply_draft_to_production"
            save_runtime_state(state)

            self._send_json(
                {
                    "status": "success",
                    "target": "production",
                    "campaign_id": str(campaign_id),
                    "cleanup": cleanup,
                    "created": {
                        "ad_group_ids": created["created_ad_group_ids"],
                        "ad_ids": created["created_ad_ids"],
                        "ad_groups": created["created_ad_groups"],
                        "ads": created["created_ads"],
                        "errors": created["errors"],
                        "region_ids": collected_payload["region_ids"],
                    },
                    "warnings": warnings,
                },
                200,
            )
            return

        if self.path == "/preview_competitor_analysis":
            theme = normalize_non_empty_string(data.get("theme"))
            if theme is None:
                self._send_json({"status": "error", "message": "theme must be a non-empty string"}, 400)
                return

            raw_competitors = data.get("competitors", DEFAULT_COMPETITOR_ANALYSIS_URLS)
            if not isinstance(raw_competitors, list) or not raw_competitors:
                self._send_json(
                    {
                        "status": "error",
                        "message": "competitors must be a non-empty array of allowed competitor URLs",
                    },
                    400,
                )
                return

            competitors: list[str] = []
            for item in raw_competitors:
                normalized_item = normalize_non_empty_string(item)
                if normalized_item is None:
                    self._send_json(
                        {
                            "status": "error",
                            "message": "each competitor URL must be a non-empty string",
                        },
                        400,
                    )
                    return
                competitors.append(normalized_item)

            preview_result = build_competitor_analysis_preview_payload(theme=theme, competitors=competitors)
            self._send_json(preview_result["payload"], preview_result["status"])
            return

        if self.path == "/preview_campaign_enrichment":
            campaign_id = normalize_campaign_id(data.get("campaign_id"))
            if campaign_id is None:
                self._send_json({"status": "error", "message": "campaign_id must be a positive integer or numeric string"}, 400)
                return

            theme = normalize_non_empty_string(data.get("theme"))
            client = YandexDirectClient.for_target("production")

            try:
                preview_result = build_campaign_enrichment_preview_payload(
                    client=client,
                    campaign_id=campaign_id,
                    requested_theme=theme,
                )
            except YandexDirectClientError as e:
                self._send_json(
                    {"status": "error", "message": str(e), "target": "production", "campaign_id": str(campaign_id)},
                    502,
                )
                return

            if not preview_result["ok"]:
                self._send_json(preview_result["payload"], preview_result["status"])
                return

            self._send_json(
                {
                    "status": "success",
                    "target": "production",
                    "campaign_id": str(campaign_id),
                    "theme": preview_result["payload"]["theme"],
                    "preview": preview_result["payload"]["preview"],
                    "warnings": preview_result["payload"]["warnings"],
                    "not_confirmed": preview_result["payload"]["not_confirmed"],
                },
                200,
            )
            return

        if self.path == "/apply_campaign_enrichment":
            confirm = resolve_confirm(data)
            if not confirm:
                self._send_json(
                    {"status": "error", "message": "apply_campaign_enrichment requires explicit confirm=true", "target": "production"},
                    400,
                )
                return

            campaign_id = normalize_campaign_id(data.get("campaign_id"))
            if campaign_id is None:
                self._send_json({"status": "error", "message": "campaign_id must be a positive integer or numeric string"}, 400)
                return

            theme = normalize_non_empty_string(data.get("theme"))
            client = YandexDirectClient.for_target("production")

            try:
                preview_result = build_campaign_enrichment_preview_payload(
                    client=client,
                    campaign_id=campaign_id,
                    requested_theme=theme,
                )
            except YandexDirectClientError as e:
                self._send_json(
                    {"status": "error", "message": str(e), "target": "production", "campaign_id": str(campaign_id)},
                    502,
                )
                return

            if not preview_result["ok"]:
                self._send_json(preview_result["payload"], preview_result["status"])
                return

            preview_payload = preview_result["payload"]
            preview = preview_payload["preview"]
            warnings = list(preview_payload["warnings"])
            applied = {}
            errors = []
            keywords_added: list[dict] = []
            current_counter_ids = []
            for item in preview_payload["campaign"].get("UnifiedCampaign", {}).get("CounterIds", {}).get("Items", []):
                normalized_item = normalize_positive_int_id(item)
                if normalized_item is not None:
                    current_counter_ids.append(normalized_item)
            current_negative_keyword_shared_set_ids = []
            for item in (
                preview_payload["campaign"]
                .get("UnifiedCampaign", {})
                .get("NegativeKeywordSharedSetIds", {})
                .get("Items", [])
            ):
                normalized_item = normalize_positive_int_id(item)
                if normalized_item is not None:
                    current_negative_keyword_shared_set_ids.append(normalized_item)

            try:
                tracking_result = client.update_campaign_tracking_params(
                    campaign_id=campaign_id,
                    tracking_params=preview["tracking_params"],
                    counter_ids=current_counter_ids,
                    negative_keyword_shared_set_ids=current_negative_keyword_shared_set_ids,
                )
                parsed_tracking = parse_update_result(tracking_result, "campaign")
                if parsed_tracking["ok"]:
                    applied["tracking_params"] = preview["tracking_params"]
                else:
                    errors.append({"step": "tracking_params", "error": parsed_tracking["payload"]})
            except YandexDirectClientError as e:
                errors.append({"step": "tracking_params", "message": str(e)})

            try:
                sitelinks_result = client.add_sitelinks(
                    sitelinks=[
                        {
                            "Title": item["title"],
                            "Href": item["href"],
                        }
                        for item in preview["sitelinks"]
                    ]
                )
                parsed_sitelinks = parse_add_result(sitelinks_result, "sitelink_set")
                if parsed_sitelinks["ok"]:
                    sitelink_set_id = int(parsed_sitelinks["payload"]["id"])
                    attached_sitelinks = attach_sitelink_set_to_ads(
                        client=client,
                        ad_ids=preview_payload["ad_ids"],
                        sitelink_set_id=sitelink_set_id,
                    )
                    applied["sitelink_set_id"] = str(sitelink_set_id)
                    applied["sitelinks"] = preview["sitelinks"]
                    applied["sitelinks_attached_ad_ids"] = attached_sitelinks["updated_ad_ids"]
                    if attached_sitelinks["errors"]:
                        warnings.append("Could not attach the new sitelink set to some existing ads.")
                        errors.append({"step": "attach_sitelinks_to_ads", "errors": attached_sitelinks["errors"]})
                    if not preview_payload["ad_ids"]:
                        warnings.append("Current campaign has no ads available for sitelink attachment.")
                else:
                    errors.append({"step": "create_sitelinks", "error": parsed_sitelinks["payload"]})
            except YandexDirectClientError as e:
                errors.append({"step": "create_sitelinks", "message": str(e)})

            callouts_result = ensure_callouts_available(
                client=client,
                callouts=preview["callouts"],
            )
            if callouts_result["ok"]:
                callout_payload = callouts_result["payload"]
                if callout_payload["errors"]:
                    warnings.append("Some callouts could not be created and were skipped.")
                    errors.append({"step": "create_callouts", "errors": callout_payload["errors"]})
                if callout_payload["callout_ids"]:
                    attached_callouts = attach_callouts_to_ads(
                        client=client,
                        ad_ids=preview_payload["ad_ids"],
                        existing_ad_extension_ids_by_ad_id=preview_payload.get("ad_extension_ids_by_ad_id", {}),
                        callout_ids=callout_payload["callout_ids"],
                    )
                    applied["callout_apply_mechanism"] = "AdExtensions.add/get + Ads.update(TextAd.CalloutSetting)"
                    applied["callout_ids"] = [str(item) for item in callout_payload["callout_ids"]]
                    applied["callouts"] = preview["callouts"]
                    applied["callouts_attached_ad_ids"] = attached_callouts["updated_ad_ids"]
                    if attached_callouts["warnings"]:
                        warnings.extend(attached_callouts["warnings"])
                    if attached_callouts["errors"]:
                        warnings.append("Could not attach the new callouts to some existing ads.")
                        errors.append({"step": "attach_callouts_to_ads", "errors": attached_callouts["errors"]})
                    if not preview_payload["ad_ids"]:
                        warnings.append("Current campaign has no ads available for callout attachment.")
            else:
                errors.append({"step": "create_callouts", "error": callouts_result["payload"]})

            group_negative_keywords_updated_ad_group_ids: list[str] = []
            group_negative_keywords_errors: list[dict] = []
            for item in preview.get("group_negative_keywords", []):
                if not isinstance(item, dict):
                    continue
                ad_group_id = normalize_positive_int_id(item.get("ad_group_id"))
                if ad_group_id is None:
                    continue

                current_negative_keywords = None
                for raw_ad_group in preview_payload.get("ad_groups", []):
                    if not isinstance(raw_ad_group, dict):
                        continue
                    raw_ad_group_id = normalize_positive_int_id(raw_ad_group.get("Id") or raw_ad_group.get("id"))
                    if raw_ad_group_id != ad_group_id:
                        continue
                    current_negative_keywords = normalize_negative_keywords(
                        raw_ad_group.get("NegativeKeywords", {}).get("Items")
                        if isinstance(raw_ad_group.get("NegativeKeywords"), dict)
                        else raw_ad_group.get("negative_keywords")
                    ) or []
                    break

                prepared_negative_keywords = normalize_negative_keywords(item.get("negative_keywords")) or []
                merged_negative_keywords = []
                seen_negative_keywords = set()
                for keyword in (current_negative_keywords or []) + prepared_negative_keywords:
                    normalized_keyword = normalize_non_empty_string(keyword)
                    if normalized_keyword is None or normalized_keyword in seen_negative_keywords:
                        continue
                    seen_negative_keywords.add(normalized_keyword)
                    merged_negative_keywords.append(normalized_keyword)

                if current_negative_keywords == merged_negative_keywords:
                    continue

                try:
                    update_group_result = client.update_ad_group_negative_keywords(
                        ad_group_id=ad_group_id,
                        negative_keywords=merged_negative_keywords,
                    )
                except YandexDirectClientError as e:
                    group_negative_keywords_errors.append(
                        {
                            "ad_group_id": str(ad_group_id),
                            "message": str(e),
                        }
                    )
                    continue

                parsed_update_group = parse_update_result(update_group_result, "ad_group")
                if not parsed_update_group["ok"]:
                    group_negative_keywords_errors.append(
                        {
                            "ad_group_id": str(ad_group_id),
                            "error": parsed_update_group["payload"],
                        }
                    )
                    continue

                group_negative_keywords_updated_ad_group_ids.append(str(ad_group_id))

            if preview.get("group_negative_keywords"):
                applied["group_negative_keywords"] = preview["group_negative_keywords"]
                applied["group_negative_keywords_updated_ad_group_ids"] = group_negative_keywords_updated_ad_group_ids
                if group_negative_keywords_errors:
                    warnings.append("Could not apply group-level negative keywords to some ad groups.")
                    errors.append({"step": "apply_group_negative_keywords", "errors": group_negative_keywords_errors})

            keyword_errors: list[dict] = []
            for item in preview.get("keywords", []):
                if not isinstance(item, dict):
                    continue
                ad_group_id = normalize_positive_int_id(item.get("ad_group_id"))
                if ad_group_id is None:
                    continue

                prepared_keywords = []
                for raw_keyword in item.get("keywords", []):
                    normalized_keyword = normalize_campaign_enrichment_keyword_phrase(raw_keyword)
                    if normalized_keyword is None or normalized_keyword == "---autotargeting":
                        continue
                    prepared_keywords.append({"AdGroupId": ad_group_id, "Keyword": normalized_keyword})

                if not prepared_keywords:
                    continue

                try:
                    add_keywords_result = client.add_keywords(prepared_keywords)
                except YandexDirectClientError as e:
                    keyword_errors.append(
                        {
                            "ad_group_id": str(ad_group_id),
                            "message": str(e),
                        }
                    )
                    continue

                raw_add_results = add_keywords_result.get("result", {}).get("AddResults", [])
                if not isinstance(raw_add_results, list) or not raw_add_results:
                    keyword_errors.append(
                        {
                            "ad_group_id": str(ad_group_id),
                            "message": "empty AddResults from Yandex Direct for keywords",
                            "raw": add_keywords_result,
                        }
                    )
                    continue

                current_keyword_ids: list[str] = []
                current_keyword_errors: list[dict] = []
                current_keyword_warnings: list[dict] = []

                for index, action_result in enumerate(raw_add_results):
                    source_keyword = prepared_keywords[index]["Keyword"] if index < len(prepared_keywords) else None
                    if not isinstance(action_result, dict):
                        current_keyword_errors.append(
                            {
                                "keyword": source_keyword,
                                "message": "invalid AddResults item",
                                "raw": action_result,
                            }
                        )
                        continue

                    if action_result.get("Warnings"):
                        current_keyword_warnings.append(
                            {
                                "keyword": source_keyword,
                                "warnings": action_result.get("Warnings", []),
                            }
                        )

                    if action_result.get("Errors"):
                        current_keyword_errors.append(
                            {
                                "keyword": source_keyword,
                                "errors": action_result.get("Errors", []),
                            }
                        )
                        continue

                    keyword_id = normalize_positive_int_id(action_result.get("Id"))
                    if keyword_id is None:
                        current_keyword_errors.append(
                            {
                                "keyword": source_keyword,
                                "message": "keyword id is missing in AddResults",
                                "raw": action_result,
                            }
                        )
                        continue

                    keyword_id_as_text = str(keyword_id)
                    if keyword_id_as_text not in current_keyword_ids:
                        current_keyword_ids.append(keyword_id_as_text)

                if current_keyword_ids:
                    keywords_added.append(
                        {
                            "ad_group_id": str(ad_group_id),
                            "keyword_ids": current_keyword_ids,
                        }
                    )

                if current_keyword_warnings:
                    warnings.append(f"Yandex Direct returned warnings for some keywords in ad_group {ad_group_id}.")

                if current_keyword_errors:
                    keyword_errors.append(
                        {
                            "ad_group_id": str(ad_group_id),
                            "errors": current_keyword_errors,
                        }
                    )

            if keywords_added:
                applied["keywords"] = preview.get("keywords", [])

            if preview.get("keywords") and keyword_errors:
                warnings.append("Could not add some keywords to some ad groups.")
                errors.append({"step": "add_keywords", "errors": keyword_errors})

            try:
                negative_set_name = f"OpenClaw {preview_payload['theme']} {datetime.utcnow().strftime('%Y%m%d%H%M%S')}"
                negative_set_result = client.add_negative_keyword_shared_set(
                    name=negative_set_name,
                    negative_keywords=preview["negative_keywords"],
                )
                parsed_negative_set = parse_add_result(negative_set_result, "negative_keyword_shared_set")
                if parsed_negative_set["ok"]:
                    shared_set_id = normalize_positive_int_id(parsed_negative_set["payload"]["id"])
                    current_shared_set_ids = []
                    for item in preview_payload["campaign"].get("UnifiedCampaign", {}).get("NegativeKeywordSharedSetIds", {}).get("Items", []):
                        normalized_item = normalize_positive_int_id(item)
                        if normalized_item is not None:
                            current_shared_set_ids.append(normalized_item)

                    if shared_set_id is not None and shared_set_id not in current_shared_set_ids:
                        updated_shared_set_ids = current_shared_set_ids + [shared_set_id]
                    else:
                        updated_shared_set_ids = current_shared_set_ids

                    if len(updated_shared_set_ids) > 3:
                        warnings.append("NegativeKeywordSharedSetIds limit prevented attaching the new shared set to the campaign.")
                        errors.append(
                            {
                                "step": "attach_negative_keywords",
                                "message": "NegativeKeywordSharedSetIds supports at most 3 items",
                            }
                        )
                    elif shared_set_id is not None:
                        attach_result = client.update_campaign_negative_keyword_shared_set_ids(
                            campaign_id=campaign_id,
                            shared_set_ids=updated_shared_set_ids,
                        )
                        parsed_attach = parse_update_result(attach_result, "campaign")
                        if parsed_attach["ok"]:
                            applied["shared_set_id"] = str(shared_set_id)
                            applied["shared_set_ids"] = [str(item) for item in updated_shared_set_ids]
                            applied["negative_keywords"] = preview["negative_keywords"]
                        else:
                            errors.append({"step": "attach_negative_keywords", "error": parsed_attach["payload"]})
                else:
                    errors.append({"step": "create_negative_keyword_shared_set", "error": parsed_negative_set["payload"]})
            except YandexDirectClientError as e:
                errors.append({"step": "create_negative_keyword_shared_set", "message": str(e)})

            if not applied:
                self._send_json(
                    {
                        "status": "error",
                        "message": "campaign enrichment did not apply any confirmed changes",
                        "target": "production",
                        "campaign_id": str(campaign_id),
                        "theme": preview_payload["theme"],
                        "errors": errors,
                        "warnings": warnings,
                        "keywords_added": keywords_added,
                        "not_confirmed": preview_payload["not_confirmed"],
                    },
                    400,
                )
                return

            self._send_json(
                {
                    "status": "success",
                    "target": "production",
                    "campaign_id": str(campaign_id),
                    "theme": preview_payload["theme"],
                    "applied": applied,
                    "warnings": warnings,
                    "errors": errors,
                    "keywords_added": keywords_added,
                    "not_confirmed": preview_payload["not_confirmed"],
                },
                200,
            )
            return

        if self.path == "/create_sitelinks":
            target = resolve_target(data)
            if target not in ALLOWED_TARGETS:
                self._send_json({"status": "error", "message": "target must be 'sandbox' or 'production'"}, 400)
                return

            confirm = resolve_confirm(data)
            if target == "production" and not confirm:
                self._send_json(
                    {"status": "error", "message": "production sitelinks create requires explicit confirm=true", "target": "production"},
                    400,
                )
                return

            raw_sitelinks = data.get("sitelinks")
            if not isinstance(raw_sitelinks, list):
                self._send_json({"status": "error", "message": "sitelinks must be an array"}, 400)
                return

            sitelinks = []
            for item in raw_sitelinks:
                if not isinstance(item, dict):
                    self._send_json({"status": "error", "message": "each sitelink must be an object"}, 400)
                    return

                title = item.get("title")
                if not isinstance(title, str) or not title.strip():
                    self._send_json({"status": "error", "message": "each sitelink title must be a non-empty string"}, 400)
                    return

                href = item.get("href")
                if not isinstance(href, str) or not href.strip():
                    self._send_json({"status": "error", "message": "each sitelink href must be a non-empty string"}, 400)
                    return

                sitelinks.append(
                    {
                        "Title": title.strip(),
                        "Href": href.strip(),
                    }
                )

            client = YandexDirectClient.for_target(target)

            try:
                result = client.add_sitelinks(sitelinks=sitelinks)
            except YandexDirectClientError as e:
                self._send_json(
                    {"status": "error", "message": str(e), "target": target},
                    502,
                )
                return

            parsed = parse_add_result(result, "sitelink_set")
            if not parsed["ok"]:
                self._send_json(parsed["payload"], parsed["status"])
                return

            self._send_json(
                {
                    "status": "success",
                    "sitelink_set_id": parsed["payload"]["id"],
                    "raw": result,
                },
                200,
            )
            return

        if self.path == "/create_negative_keyword_shared_set":
            target = resolve_target(data)
            if target not in ALLOWED_TARGETS:
                self._send_json({"status": "error", "message": "target must be 'sandbox' or 'production'"}, 400)
                return

            negative_keywords = normalize_negative_keywords(data.get("negative_keywords"))
            if negative_keywords is None or not negative_keywords:
                self._send_json({"status": "error", "message": "negative_keywords must be a non-empty array of strings"}, 400)
                return

            confirm = resolve_confirm(data)
            if target == "production" and not confirm:
                self._send_json(
                    {
                        "status": "error",
                        "message": "production negative keyword shared set create requires explicit confirm=true",
                        "target": "production",
                    },
                    400,
                )
                return

            shared_set_name = f"OpenClaw Negative Keywords {datetime.utcnow().strftime('%Y%m%d%H%M%S')}"
            client = YandexDirectClient.for_target(target)

            try:
                result = client.add_negative_keyword_shared_set(
                    name=shared_set_name,
                    negative_keywords=negative_keywords,
                )
            except YandexDirectClientError as e:
                self._send_json(
                    {"status": "error", "message": str(e), "target": target},
                    502,
                )
                return

            parsed = parse_add_result(result, "negative_keyword_shared_set")
            if not parsed["ok"]:
                payload = parsed["payload"]
                payload["target"] = target
                self._send_json(payload, parsed["status"])
                return

            self._send_json(
                {
                    "status": "success",
                    "shared_set_id": parsed["payload"]["id"],
                },
                200,
            )
            return

        if self.path == "/attach_negative_keyword_shared_set_to_campaign":
            target = resolve_target(data)
            if target not in ALLOWED_TARGETS:
                self._send_json({"status": "error", "message": "target must be 'sandbox' or 'production'"}, 400)
                return

            campaign_id = normalize_campaign_id(data.get("campaign_id"))
            if campaign_id is None:
                self._send_json({"status": "error", "message": "campaign_id must be a positive integer or numeric string"}, 400)
                return

            shared_set_id = normalize_positive_int_id(data.get("shared_set_id"))
            if shared_set_id is None:
                self._send_json({"status": "error", "message": "shared_set_id must be a positive integer or numeric string"}, 400)
                return

            confirm = resolve_confirm(data)
            if target == "production" and not confirm:
                self._send_json(
                    {
                        "status": "error",
                        "message": "production campaign negative keyword shared set attach requires explicit confirm=true",
                        "target": "production",
                    },
                    400,
                )
                return

            client = YandexDirectClient.for_target(target)

            try:
                current_result = client.get_campaign_details(campaign_id)
            except YandexDirectClientError as e:
                self._send_json(
                    {"status": "error", "message": str(e), "target": target, "campaign_id": str(campaign_id)},
                    502,
                )
                return

            campaigns = current_result.get("result", {}).get("Campaigns", [])
            if not campaigns:
                self._send_json(
                    {"status": "error", "message": "campaign not found", "target": target, "campaign_id": str(campaign_id), "raw": current_result},
                    404,
                )
                return

            current_shared_set_ids_raw = (
                campaigns[0]
                .get("UnifiedCampaign", {})
                .get("NegativeKeywordSharedSetIds", {})
                .get("Items", [])
            )

            current_shared_set_ids = []
            for item in current_shared_set_ids_raw:
                normalized_item = normalize_positive_int_id(item)
                if normalized_item is not None:
                    current_shared_set_ids.append(normalized_item)

            if shared_set_id not in current_shared_set_ids:
                updated_shared_set_ids = current_shared_set_ids + [shared_set_id]
            else:
                updated_shared_set_ids = current_shared_set_ids

            if len(updated_shared_set_ids) > 3:
                self._send_json(
                    {"status": "error", "message": "NegativeKeywordSharedSetIds supports at most 3 items"},
                    400,
                )
                return

            try:
                result = client.update_campaign_negative_keyword_shared_set_ids(
                    campaign_id=campaign_id,
                    shared_set_ids=updated_shared_set_ids,
                )
            except YandexDirectClientError as e:
                self._send_json(
                    {"status": "error", "message": str(e), "target": target, "campaign_id": str(campaign_id)},
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

            self._send_json(
                {
                    "status": "success",
                    "campaign_id": str(campaign_id),
                    "shared_set_id": str(shared_set_id),
                    "shared_set_ids": [str(item) for item in updated_shared_set_ids],
                },
                200,
            )
            return

        if self.path == "/set_campaign_tracking_params":
            target = resolve_target(data)
            if target not in ALLOWED_TARGETS:
                self._send_json({"status": "error", "message": "target must be 'sandbox' or 'production'"}, 400)
                return

            campaign_id = normalize_campaign_id(data.get("campaign_id"))
            if campaign_id is None:
                self._send_json({"status": "error", "message": "campaign_id must be a positive integer or numeric string"}, 400)
                return

            tracking_params = data.get("tracking_params")
            if not isinstance(tracking_params, str) or not tracking_params.strip():
                self._send_json({"status": "error", "message": "tracking_params must be a non-empty string"}, 400)
                return
            tracking_params = tracking_params.strip()

            confirm = resolve_confirm(data)
            if target == "production" and not confirm:
                self._send_json(
                    {
                        "status": "error",
                        "message": "production campaign tracking params update requires explicit confirm=true",
                        "target": "production",
                    },
                    400,
                )
                return

            client = YandexDirectClient.for_target(target)

            try:
                current_result = client.get_campaign_details(campaign_id)
            except YandexDirectClientError as e:
                self._send_json(
                    {"status": "error", "message": str(e), "target": target, "campaign_id": str(campaign_id)},
                    502,
                )
                return

            campaigns = current_result.get("result", {}).get("Campaigns", [])
            if not campaigns:
                self._send_json(
                    {"status": "error", "message": "campaign not found", "target": target, "campaign_id": str(campaign_id), "raw": current_result},
                    404,
                )
                return

            unified_campaign = campaigns[0].get("UnifiedCampaign", {})

            current_counter_ids = []
            for item in unified_campaign.get("CounterIds", {}).get("Items", []):
                normalized_item = normalize_positive_int_id(item)
                if normalized_item is not None:
                    current_counter_ids.append(normalized_item)

            current_shared_set_ids = []
            for item in unified_campaign.get("NegativeKeywordSharedSetIds", {}).get("Items", []):
                normalized_item = normalize_positive_int_id(item)
                if normalized_item is not None:
                    current_shared_set_ids.append(normalized_item)

            try:
                result = client.update_campaign_tracking_params(
                    campaign_id=campaign_id,
                    tracking_params=tracking_params,
                    counter_ids=current_counter_ids,
                    negative_keyword_shared_set_ids=current_shared_set_ids,
                )
            except YandexDirectClientError as e:
                self._send_json(
                    {"status": "error", "message": str(e), "target": target, "campaign_id": str(campaign_id)},
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

            self._send_json(
                {
                    "status": "success",
                    "campaign_id": str(campaign_id),
                    "tracking_params": tracking_params,
                },
                200,
            )
            return

        if self.path == "/get_autotargeting":
            target = resolve_target(data)
            if target not in ALLOWED_TARGETS:
                self._send_json({"status": "error", "message": "target must be 'sandbox' or 'production'"}, 400)
                return

            ad_group_id = normalize_ad_group_id(data.get("ad_group_id"))
            if ad_group_id is None:
                self._send_json({"status": "error", "message": "ad_group_id must be a positive integer or numeric string"}, 400)
                return

            client = YandexDirectClient.for_target(target)

            try:
                result = client.get_autotargeting_keywords(ad_group_id)
            except YandexDirectClientError as e:
                self._send_json(
                    {"status": "error", "message": str(e), "target": target, "ad_group_id": str(ad_group_id)},
                    502,
                )
                return

            raw_keywords = result.get("result", {}).get("Keywords", [])
            autotargetings = []

            for keyword in raw_keywords:
                if keyword.get("Keyword") != "---autotargeting":
                    continue

                autotargetings.append(
                    {
                        "id": str(keyword.get("Id")),
                        "ad_group_id": str(keyword.get("AdGroupId")),
                        "campaign_id": str(keyword.get("CampaignId")),
                        "status": keyword.get("Status"),
                        "state": keyword.get("State"),
                        "serving_status": keyword.get("ServingStatus"),
                        "bid": keyword.get("Bid"),
                        "context_bid": keyword.get("ContextBid"),
                        "strategy_priority": keyword.get("StrategyPriority"),
                        "autotargeting_search_bid_is_auto": keyword.get("AutotargetingSearchBidIsAuto"),
                        "autotargeting_settings": keyword.get("AutotargetingSettings"),
                    }
                )

            self._send_json(
                {
                    "status": "success",
                    "target": target,
                    "ad_group_id": str(ad_group_id),
                    "autotargetings": autotargetings,
                },
                200,
            )
            return

        if self.path == "/update_autotargeting":
            target = resolve_target(data)
            if target not in ALLOWED_TARGETS:
                self._send_json({"status": "error", "message": "target must be 'sandbox' or 'production'"}, 400)
                return

            ad_group_id = normalize_ad_group_id(data.get("ad_group_id"))
            if ad_group_id is None:
                self._send_json({"status": "error", "message": "ad_group_id must be a positive integer or numeric string"}, 400)
                return

            autotargeting_settings = normalize_autotargeting_settings(data.get("autotargeting_settings"))
            if autotargeting_settings is None:
                self._send_json(
                    {
                        "status": "error",
                        "message": "autotargeting_settings must be an object with confirmed Categories and/or BrandOptions values YES/NO",
                    },
                    400,
                )
                return

            confirm = resolve_confirm(data)
            if target == "production" and not confirm:
                self._send_json(
                    {
                        "status": "error",
                        "message": "production autotargeting update requires explicit confirm=true",
                        "target": "production",
                    },
                    400,
                )
                return

            client = YandexDirectClient.for_target(target)

            try:
                current_result = client.get_autotargeting_keywords(ad_group_id)
            except YandexDirectClientError as e:
                self._send_json(
                    {"status": "error", "message": str(e), "target": target, "ad_group_id": str(ad_group_id)},
                    502,
                )
                return

            raw_keywords = current_result.get("result", {}).get("Keywords", [])
            autotargetings = [keyword for keyword in raw_keywords if keyword.get("Keyword") == "---autotargeting"]

            if not autotargetings:
                self._send_json(
                    {"status": "error", "message": "autotargeting not found", "target": target, "ad_group_id": str(ad_group_id)},
                    404,
                )
                return

            if len(autotargetings) > 1:
                self._send_json(
                    {
                        "status": "error",
                        "message": "multiple autotargeting objects found; cannot confirm which one to update",
                        "target": target,
                        "ad_group_id": str(ad_group_id),
                    },
                    409,
                )
                return

            current_autotargeting = autotargetings[0]
            autotargeting_id = normalize_positive_int_id(current_autotargeting.get("Id"))
            if autotargeting_id is None:
                self._send_json(
                    {"status": "error", "message": "autotargeting id is missing in Keywords.get response", "raw": current_autotargeting},
                    502,
                )
                return

            merged_settings = {}
            current_settings = current_autotargeting.get("AutotargetingSettings")

            current_categories = current_settings.get("Categories") if isinstance(current_settings, dict) else None
            if isinstance(current_categories, dict) and current_categories:
                merged_settings["Categories"] = dict(current_categories)

            current_brand_options = current_settings.get("BrandOptions") if isinstance(current_settings, dict) else None
            if isinstance(current_brand_options, dict) and current_brand_options:
                merged_settings["BrandOptions"] = dict(current_brand_options)

            if "Categories" in autotargeting_settings:
                merged_settings.setdefault("Categories", {}).update(autotargeting_settings["Categories"])

            if "BrandOptions" in autotargeting_settings:
                merged_settings.setdefault("BrandOptions", {}).update(autotargeting_settings["BrandOptions"])

            try:
                result = client.update_autotargeting_settings(
                    autotargeting_id=autotargeting_id,
                    autotargeting_settings=merged_settings,
                )
            except YandexDirectClientError as e:
                self._send_json(
                    {"status": "error", "message": str(e), "target": target, "ad_group_id": str(ad_group_id)},
                    502,
                )
                return

            parsed = parse_update_result(result, "autotargeting")
            if not parsed["ok"]:
                payload = parsed["payload"]
                payload["target"] = target
                payload["ad_group_id"] = str(ad_group_id)
                self._send_json(payload, parsed["status"])
                return

            self._send_json(
                {
                    "status": "success",
                    "target": target,
                    "ad_group_id": str(ad_group_id),
                    "autotargeting_id": str(autotargeting_id),
                    "autotargeting_settings": merged_settings,
                },
                200,
            )
            return

        if self.path == "/upload_ad_image":
            target = resolve_target(data)
            if target not in ALLOWED_TARGETS:
                self._send_json({"status": "error", "message": "target must be 'sandbox' or 'production'"}, 400)
                return

            confirm = resolve_confirm(data)
            if target == "production" and not confirm:
                self._send_json(
                    {"status": "error", "message": "production ad image upload requires explicit confirm=true", "target": "production"},
                    400,
                )
                return

            name = normalize_non_empty_string(data.get("name"))
            if name is None:
                self._send_json({"status": "error", "message": "name must be a non-empty string"}, 400)
                return

            image_data_base64 = normalize_non_empty_string(data.get("image_data_base64"))
            if image_data_base64 is None:
                self._send_json({"status": "error", "message": "image_data_base64 must be a non-empty string"}, 400)
                return

            try:
                base64.b64decode(image_data_base64, validate=True)
            except (binascii.Error, ValueError):
                self._send_json({"status": "error", "message": "image_data_base64 must be valid base64"}, 400)
                return

            client = YandexDirectClient.for_target(target)

            try:
                result = client.add_ad_image(name=name, image_data_base64=image_data_base64)
            except YandexDirectClientError as e:
                self._send_json({"status": "error", "message": str(e), "target": target}, 502)
                return

            parsed = parse_ad_image_add_result(result)
            if not parsed["ok"]:
                payload = parsed["payload"]
                payload["target"] = target
                self._send_json(payload, parsed["status"])
                return

            state = deep_copy_json(RUNTIME_STATE)
            if not isinstance(state.get("media_library"), list):
                state["media_library"] = []
            state["last_uploaded_image_hash"] = parsed["payload"]["ad_image_hash"]
            state["media_library"].append(
                {
                    "type": "image",
                    "source": "yandex_direct_upload",
                    "ad_image_hash": parsed["payload"]["ad_image_hash"],
                }
            )
            state["draft_meta"]["last_action"] = "upload_ad_image"
            save_runtime_state(state)

            self._send_json(
                {
                    "status": "success",
                    "target": target,
                    "ad_image_hash": parsed["payload"]["ad_image_hash"],
                    "last_uploaded_image_hash": state["last_uploaded_image_hash"],
                    "media_library": state["media_library"],
                    "warnings": parsed["payload"]["warnings"],
                },
                200,
            )
            return

        if self.path == "/create_ad":
            target = resolve_target(data)
            if target not in ALLOWED_TARGETS:
                self._send_json({"status": "error", "message": "target must be 'sandbox' or 'production'"}, 400)
                return

            ad_group_id = normalize_ad_group_id(data.get("ad_group_id"))
            if ad_group_id is None:
                self._send_json({"status": "error", "message": "ad_group_id must be a positive integer or numeric string"}, 400)
                return

            title = data.get("title")
            if not isinstance(title, str) or not title.strip():
                self._send_json({"status": "error", "message": "title must be a non-empty string"}, 400)
                return

            text = data.get("text")
            if not isinstance(text, str) or not text.strip():
                self._send_json({"status": "error", "message": "text must be a non-empty string"}, 400)
                return

            href = data.get("href")
            if not isinstance(href, str) or not href.strip():
                self._send_json({"status": "error", "message": "href must be a non-empty string"}, 400)
                return

            display_url_path = data.get("display_url_path")
            if display_url_path is not None:
                if not isinstance(display_url_path, str) or not display_url_path.strip():
                    self._send_json({"status": "error", "message": "display_url_path must be a non-empty string when provided"}, 400)
                    return
                display_url_path = display_url_path.strip()

            sitelink_set_id = None
            if "sitelink_set_id" in data:
                sitelink_set_id = normalize_positive_int_id(data.get("sitelink_set_id"))
                if sitelink_set_id is None:
                    self._send_json({"status": "error", "message": "sitelink_set_id must be a positive integer or numeric string when provided"}, 400)
                    return

            ad_image_hash = None
            if "ad_image_hash" in data:
                ad_image_hash = normalize_non_empty_string(data.get("ad_image_hash"))
                if ad_image_hash is None:
                    self._send_json({"status": "error", "message": "ad_image_hash must be a non-empty string when provided"}, 400)
                    return

            confirm = resolve_confirm(data)
            if target == "production" and not confirm:
                self._send_json(
                    {"status": "error", "message": "production ad create requires explicit confirm=true", "target": "production"},
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
                        ad_image_hash=ad_image_hash,
                        sitelink_set_id=sitelink_set_id,
                    )
                else:
                    result = client.add_text_ad_production(
                        ad_group_id=ad_group_id,
                        title=title.strip(),
                        text=text.strip(),
                        href=href.strip(),
                        display_url_path=display_url_path,
                        ad_image_hash=ad_image_hash,
                        sitelink_set_id=sitelink_set_id,
                    )
            except YandexDirectClientError as e:
                self._send_json(
                    {"status": "error", "message": str(e), "target": target, "ad_group_id": str(ad_group_id)},
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

            ad_details = None
            try:
                ad_result = client.get_ad_details(int(parsed["payload"]["id"]))
                ads = ad_result.get("result", {}).get("Ads", [])
                if ads:
                    ad_details = ads[0]
            except YandexDirectClientError:
                ad_details = None

            response_payload = {
                "status": "success",
                "ad_id": parsed["payload"]["id"],
                "ad_group_id": str(ad_group_id),
                "target": target,
                "data": data,
            }
            if ad_details is not None:
                response_payload["ad"] = ad_details

            self._send_json(response_payload, 200)
            return

        if self.path == "/replace_ad_image":
            target = resolve_target(data)
            if target not in ALLOWED_TARGETS:
                self._send_json({"status": "error", "message": "target must be 'sandbox' or 'production'"}, 400)
                return

            ad_id = normalize_ad_id(data.get("ad_id"))
            if ad_id is None:
                self._send_json({"status": "error", "message": "ad_id must be a positive integer or numeric string"}, 400)
                return

            ad_image_hash = normalize_non_empty_string(data.get("ad_image_hash"))
            if ad_image_hash is None:
                self._send_json({"status": "error", "message": "ad_image_hash must be a non-empty string"}, 400)
                return

            confirm = resolve_confirm(data)
            if target == "production" and not confirm:
                self._send_json(
                    {"status": "error", "message": "production ad image replace requires explicit confirm=true", "target": "production"},
                    400,
                )
                return

            client = YandexDirectClient.for_target(target)

            try:
                result = client.update_text_ad_image(ad_id=ad_id, ad_image_hash=ad_image_hash)
            except YandexDirectClientError as e:
                self._send_json({"status": "error", "message": str(e), "target": target, "ad_id": str(ad_id)}, 502)
                return

            parsed = parse_update_result(result, "ad")
            if not parsed["ok"]:
                payload = parsed["payload"]
                payload["target"] = target
                payload["ad_id"] = str(ad_id)
                self._send_json(payload, parsed["status"])
                return

            ad_details = None
            try:
                ad_result = client.get_ad_details(ad_id)
                ads = ad_result.get("result", {}).get("Ads", [])
                if ads:
                    ad_details = ads[0]
            except YandexDirectClientError:
                ad_details = None

            response_payload = {
                "status": "success",
                "target": target,
                "ad_id": str(ad_id),
                "ad_image_hash": ad_image_hash,
                "warnings": parsed["payload"]["warnings"],
                "result": result,
            }
            if ad_details is not None:
                response_payload["ad"] = ad_details

            self._send_json(response_payload, 200)
            return

        if self.path == "/remove_ad_image":
            target = resolve_target(data)
            if target not in ALLOWED_TARGETS:
                self._send_json({"status": "error", "message": "target must be 'sandbox' or 'production'"}, 400)
                return

            ad_id = normalize_ad_id(data.get("ad_id"))
            if ad_id is None:
                self._send_json({"status": "error", "message": "ad_id must be a positive integer or numeric string"}, 400)
                return

            confirm = resolve_confirm(data)
            if target == "production" and not confirm:
                self._send_json(
                    {"status": "error", "message": "production ad image remove requires explicit confirm=true", "target": "production"},
                    400,
                )
                return

            client = YandexDirectClient.for_target(target)

            try:
                result = client.update_text_ad_image(ad_id=ad_id, remove=True)
            except YandexDirectClientError as e:
                self._send_json({"status": "error", "message": str(e), "target": target, "ad_id": str(ad_id)}, 502)
                return

            parsed = parse_update_result(result, "ad")
            if not parsed["ok"]:
                payload = parsed["payload"]
                payload["target"] = target
                payload["ad_id"] = str(ad_id)
                self._send_json(payload, parsed["status"])
                return

            ad_details = None
            try:
                ad_result = client.get_ad_details(ad_id)
                ads = ad_result.get("result", {}).get("Ads", [])
                if ads:
                    ad_details = ads[0]
            except YandexDirectClientError:
                ad_details = None

            response_payload = {
                "status": "success",
                "target": target,
                "ad_id": str(ad_id),
                "ad_image_hash": None,
                "warnings": parsed["payload"]["warnings"],
                "result": result,
            }
            if ad_details is not None:
                response_payload["ad"] = ad_details

            self._send_json(response_payload, 200)
            return

        if self.path == "/get_ad_status":
            target = resolve_target(data)
            if target not in ALLOWED_TARGETS:
                self._send_json({"status": "error", "message": "target must be 'sandbox' or 'production'"}, 400)
                return

            ad_id = normalize_ad_id(data.get("ad_id"))
            if ad_id is None:
                self._send_json({"status": "error", "message": "ad_id must be a positive integer or numeric string"}, 400)
                return

            client = YandexDirectClient.for_target(target)

            try:
                result = client.get_ad_details(ad_id)
            except YandexDirectClientError as e:
                self._send_json(
                    {"status": "error", "message": str(e), "target": target, "ad_id": str(ad_id)},
                    502,
                )
                return

            ads = result.get("result", {}).get("Ads", [])
            if not ads:
                self._send_json(
                    {"status": "error", "message": "ad not found", "target": target, "ad_id": str(ad_id), "raw": result},
                    404,
                )
                return

            ad = ads[0]

            self._send_json(
                {"status": "success", "target": target, "ad_id": str(ad_id), "ad": ad},
                200,
            )
            return

        if self.path == "/list_ads":
            target = resolve_target(data)
            if target not in ALLOWED_TARGETS:
                self._send_json({"status": "error", "message": "target must be 'sandbox' or 'production'"}, 400)
                return

            ad_group_id = normalize_ad_group_id(data.get("ad_group_id"))
            if ad_group_id is None:
                self._send_json({"status": "error", "message": "ad_group_id must be a positive integer or numeric string"}, 400)
                return

            client = YandexDirectClient.for_target(target)

            try:
                ad_group_result = client.get_ad_group_details(ad_group_id)
            except YandexDirectClientError as e:
                self._send_json(
                    {"status": "error", "message": str(e), "target": target, "ad_group_id": str(ad_group_id)},
                    502,
                )
                return

            ad_groups = ad_group_result.get("result", {}).get("AdGroups", [])
            if not ad_groups:
                self._send_json(
                    {
                        "status": "error",
                        "message": "ad_group not found",
                        "target": target,
                        "ad_group_id": str(ad_group_id),
                        "raw": ad_group_result,
                    },
                    404,
                )
                return

            try:
                result = client.list_ads(ad_group_id)
            except YandexDirectClientError as e:
                self._send_json(
                    {"status": "error", "message": str(e), "target": target, "ad_group_id": str(ad_group_id)},
                    502,
                )
                return

            raw_ads = result.get("result", {}).get("Ads", [])
            ads = []

            for ad in raw_ads:
                text_ad = ad.get("TextAd", {})
                raw_ad_extensions = text_ad.get("AdExtensions", [])
                ad_extensions = []
                callout_ids = []
                if isinstance(raw_ad_extensions, list):
                    for raw_ad_extension in raw_ad_extensions:
                        if not isinstance(raw_ad_extension, dict):
                            continue
                        ad_extension_id = normalize_positive_int_id(raw_ad_extension.get("AdExtensionId"))
                        ad_extension_type = normalize_non_empty_string(raw_ad_extension.get("Type"))
                        if ad_extension_id is not None:
                            callout_ids.append(str(ad_extension_id))
                        ad_extensions.append(
                            {
                                "ad_extension_id": str(ad_extension_id) if ad_extension_id is not None else None,
                                "type": ad_extension_type,
                            }
                        )
                ads.append(
                    {
                        "id": str(ad.get("Id")),
                        "campaign_id": str(ad.get("CampaignId")),
                        "ad_group_id": str(ad.get("AdGroupId")),
                        "status": ad.get("Status"),
                        "state": ad.get("State"),
                        "status_clarification": ad.get("StatusClarification"),
                        "type": ad.get("Type"),
                        "title": text_ad.get("Title"),
                        "text": text_ad.get("Text"),
                        "href": text_ad.get("Href"),
                        "display_url_path": text_ad.get("DisplayUrlPath"),
                        "ad_image_hash": text_ad.get("AdImageHash"),
                        "ad_image_moderation": text_ad.get("AdImageModeration"),
                        "sitelink_set_id": (
                            str(text_ad.get("SitelinkSetId"))
                            if text_ad.get("SitelinkSetId") is not None
                            else None
                        ),
                        "callout_ids": callout_ids,
                        "ad_extensions": ad_extensions,
                    }
                )

            self._send_json(
                {
                    "status": "success",
                    "target": target,
                    "ad_group_id": str(ad_group_id),
                    "ads": ads,
                },
                200,
            )
            return

        if self.path == "/disable_ads":
            target = resolve_target(data)
            if target not in ALLOWED_TARGETS:
                self._send_json({"status": "error", "message": "target must be 'sandbox' or 'production'"}, 400)
                return

            raw_ad_ids = data.get("ad_ids")
            if not isinstance(raw_ad_ids, list) or not raw_ad_ids:
                self._send_json({"status": "error", "message": "ad_ids must be a non-empty array of positive integers or numeric strings"}, 400)
                return

            normalized_ad_ids = []
            for item in raw_ad_ids:
                normalized = normalize_ad_id(item)
                if normalized is None:
                    self._send_json(
                        {"status": "error", "message": "each ad_id must be a positive integer or numeric string"},
                        400,
                    )
                    return
                normalized_ad_ids.append(normalized)

            confirm = resolve_confirm(data)
            if target == "production" and not confirm:
                self._send_json(
                    {"status": "error", "message": "production ads disable requires explicit confirm=true", "target": "production"},
                    400,
                )
                return

            client = YandexDirectClient.for_target(target)

            try:
                result = client.suspend_ads(normalized_ad_ids)
            except YandexDirectClientError as e:
                self._send_json(
                    {"status": "error", "message": str(e), "target": target, "ad_ids": [str(ad_id) for ad_id in normalized_ad_ids]},
                    502,
                )
                return

            parsed = parse_action_results(result, "SuspendResults", "ads", "disable")
            if not parsed["ok"]:
                payload = parsed["payload"]
                payload["target"] = target
                payload["ad_ids"] = [str(ad_id) for ad_id in normalized_ad_ids]
                self._send_json(payload, parsed["status"])
                return

            self._send_json(
                {
                    "status": "success",
                    "target": target,
                    "ad_ids": [str(ad_id) for ad_id in normalized_ad_ids],
                    "results": parsed["payload"]["results"],
                    "raw": result,
                },
                200,
            )
            return

        if self.path == "/delete_ads":
            target = resolve_target(data)
            if target not in ALLOWED_TARGETS:
                self._send_json({"status": "error", "message": "target must be 'sandbox' or 'production'"}, 400)
                return

            raw_ad_ids = data.get("ad_ids")
            if not isinstance(raw_ad_ids, list) or not raw_ad_ids:
                self._send_json({"status": "error", "message": "ad_ids must be a non-empty array of positive integers or numeric strings"}, 400)
                return

            normalized_ad_ids = []
            for item in raw_ad_ids:
                normalized = normalize_ad_id(item)
                if normalized is None:
                    self._send_json(
                        {"status": "error", "message": "each ad_id must be a positive integer or numeric string"},
                        400,
                    )
                    return
                normalized_ad_ids.append(normalized)

            confirm = resolve_confirm(data)
            if target == "production" and not confirm:
                self._send_json(
                    {"status": "error", "message": "production ads delete requires explicit confirm=true", "target": "production"},
                    400,
                )
                return

            client = YandexDirectClient.for_target(target)

            try:
                result = client.delete_ads(normalized_ad_ids)
            except YandexDirectClientError as e:
                self._send_json(
                    {"status": "error", "message": str(e), "target": target, "ad_ids": [str(ad_id) for ad_id in normalized_ad_ids]},
                    502,
                )
                return

            parsed = parse_action_results(result, "DeleteResults", "ads", "delete")
            if not parsed["ok"]:
                payload = parsed["payload"]
                payload["target"] = target
                payload["ad_ids"] = [str(ad_id) for ad_id in normalized_ad_ids]
                self._send_json(payload, parsed["status"])
                return

            self._send_json(
                {
                    "status": "success",
                    "target": target,
                    "ad_ids": [str(ad_id) for ad_id in normalized_ad_ids],
                    "results": parsed["payload"]["results"],
                    "raw": result,
                },
                200,
            )
            return

        if self.path == "/delete_ad_groups":
            target = resolve_target(data)
            if target not in ALLOWED_TARGETS:
                self._send_json({"status": "error", "message": "target must be 'sandbox' or 'production'"}, 400)
                return

            raw_ad_group_ids = data.get("ad_group_ids")
            if not isinstance(raw_ad_group_ids, list) or not raw_ad_group_ids:
                self._send_json(
                    {
                        "status": "error",
                        "message": "ad_group_ids must be a non-empty array of positive integers or numeric strings",
                    },
                    400,
                )
                return

            normalized_ad_group_ids = []
            for item in raw_ad_group_ids:
                normalized = normalize_ad_group_id(item)
                if normalized is None:
                    self._send_json(
                        {"status": "error", "message": "each ad_group_id must be a positive integer or numeric string"},
                        400,
                    )
                    return
                normalized_ad_group_ids.append(normalized)

            confirm = resolve_confirm(data)
            if target == "production" and not confirm:
                self._send_json(
                    {
                        "status": "error",
                        "message": "production ad groups delete requires explicit confirm=true",
                        "target": "production",
                    },
                    400,
                )
                return

            client = YandexDirectClient.for_target(target)

            try:
                result = client.delete_ad_groups(normalized_ad_group_ids)
            except YandexDirectClientError as e:
                self._send_json(
                    {
                        "status": "error",
                        "message": str(e),
                        "target": target,
                        "ad_group_ids": [str(ad_group_id) for ad_group_id in normalized_ad_group_ids],
                    },
                    502,
                )
                return

            parsed = parse_action_results(result, "DeleteResults", "ad_groups", "delete")
            if not parsed["ok"]:
                payload = parsed["payload"]
                payload["target"] = target
                payload["ad_group_ids"] = [str(ad_group_id) for ad_group_id in normalized_ad_group_ids]
                self._send_json(payload, parsed["status"])
                return

            self._send_json(
                {
                    "status": "success",
                    "target": target,
                    "ad_group_ids": [str(ad_group_id) for ad_group_id in normalized_ad_group_ids],
                    "results": parsed["payload"]["results"],
                    "raw": result,
                },
                200,
            )
            return

        if self.path == "/moderate_ad":
            target = resolve_target(data)
            if target not in ALLOWED_TARGETS:
                self._send_json({"status": "error", "message": "target must be 'sandbox' or 'production'"}, 400)
                return

            ad_id = normalize_ad_id(data.get("ad_id"))
            if ad_id is None:
                self._send_json({"status": "error", "message": "ad_id must be a positive integer or numeric string"}, 400)
                return

            confirm = resolve_confirm(data)
            if target == "production" and not confirm:
                self._send_json(
                    {"status": "error", "message": "production ad moderation requires explicit confirm=true", "target": "production"},
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
                    {"status": "error", "message": str(e), "target": target, "ad_id": str(ad_id)},
                    502,
                )
                return

            self._send_json(
                {"status": "success", "target": target, "ad_id": str(ad_id), "result": result},
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
