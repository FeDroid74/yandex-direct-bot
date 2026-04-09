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
from typing import Optional

from config import settings
from services.direct_client import YandexDirectClient, YandexDirectClientError
from services.direct_mock import validate_campaign


ALLOWED_TARGETS = {"sandbox", "production"}
ALLOWED_OFFER_RETARGETING = {"YES", "NO"}
ALLOWED_AUTOTARGETING_SETTINGS_VALUES = {"YES", "NO"}
ALLOWED_AUTOTARGETING_CATEGORY_KEYS = {"Exact", "Narrow", "Alternative", "Accessory", "Broader"}
ALLOWED_AUTOTARGETING_BRAND_OPTION_KEYS = {"WithoutBrands", "WithAdvertiserBrand", "WithCompetitorsBrand"}
DEFAULT_METRICA_COUNTER_ID = 99041859
DEFAULT_GOAL_ID = 352606262
STATE_FILE_PATH = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "STATE.md"))
ARTFARFOR_BASE_URL = "https://artfarfor.com/"
ARTFARFOR_DOMAIN = "artfarfor.com"
ARTFARFOR_IMAGE_ALLOWED_DOMAINS = {ARTFARFOR_DOMAIN, "static.insales-cdn.com"}
DEFAULT_SITE_IMAGE_LIMIT = 3
MAX_SITE_DISCOVERY_PAGES = 18
MAX_SITE_LINKS_PER_PAGE = 20
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
                "creative_spec": None,
            }
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
            else:
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


def link_image_hash_to_draft_state(state: dict, scope: str, ad_index: int, ad_group_index: Optional[int], ad_image_hash: str) -> dict:
    draft_campaign = state.get("draft_campaign")
    if not isinstance(draft_campaign, dict):
        return {"ok": False, "status": 409, "payload": {"status": "error", "message": "draft_campaign is not initialized"}}

    draft_ad, link_ref = resolve_draft_ad_reference(
        draft_campaign=draft_campaign,
        scope=scope,
        ad_index=ad_index,
        ad_group_index=ad_group_index,
    )
    if draft_ad is None or link_ref is None:
        return {"ok": False, "status": 400, "payload": {"status": "error", "message": "draft ad not found for provided scope/indexes"}}

    draft_ad["ad_image_hash"] = ad_image_hash
    creative_spec = state.get("creative_spec")
    if isinstance(creative_spec, dict):
        draft_ad["creative_spec"] = deep_copy_json(creative_spec)

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
                ad_groups.append(
                    {
                        "id": str(ad_group.get("Id")),
                        "name": ad_group.get("Name"),
                        "campaign_id": str(ad_group.get("CampaignId")),
                        "region_ids": ad_group.get("RegionIds", []),
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

            discovery = find_site_images_for_theme_internal(theme=theme, limit=max(match_index + 1, DEFAULT_SITE_IMAGE_LIMIT))
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

            selected_match = matches[match_index]
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

            state = deep_copy_json(RUNTIME_STATE)
            add_yandex_uploaded_image_to_state(state, upload_result["payload"]["ad_image_hash"])
            if not isinstance(state.get("media_library"), list):
                state["media_library"] = []
            state["media_library"].append(
                {
                    "type": "image",
                    "source": "artfarfor_site",
                    "theme": theme,
                    "page_url": selected_match["page_url"],
                    "image_url": selected_match["image_url"],
                }
            )

            linked = link_image_hash_to_draft_state(
                state=state,
                scope=scope,
                ad_index=ad_index,
                ad_group_index=ad_group_index,
                ad_image_hash=upload_result["payload"]["ad_image_hash"],
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
                    "selected_match": selected_match,
                    "ad_image_hash": upload_result["payload"]["ad_image_hash"],
                    "draft_fragment": linked["payload"]["draft_fragment"],
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

            draft_ad["ad_image_hash"] = None
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
