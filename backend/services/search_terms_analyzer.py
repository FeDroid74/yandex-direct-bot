from typing import Any, Optional
import re


def _normalize_query(value: Any) -> Optional[str]:
    if not isinstance(value, str):
        return None
    normalized = " ".join(value.strip().split())
    return normalized or None


def _get_value(row: dict, *keys: str):
    for key in keys:
        if key in row:
            return row[key]
    return None


def _to_int(value: Any) -> int:
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped or stripped == "--":
            return 0
        try:
            return int(float(stripped.replace(",", ".")))
        except ValueError:
            return 0
    return 0


def _to_float(value: Any) -> float:
    if isinstance(value, bool):
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped or stripped == "--":
            return 0.0
        try:
            return float(stripped.replace(",", "."))
        except ValueError:
            return 0.0
    return 0.0


def _build_output_item(item: dict) -> dict:
    return {
        "query": item["query"],
        "clicks": item["clicks"],
        "cost": round(item["cost"], 2),
        "conversions": round(item["conversions"], 4),
    }


def analyze_search_terms(rows, target_cpa: Optional[float] = None):
    aggregated: dict[str, dict] = {}

    for row in rows:
        if not isinstance(row, dict):
            continue

        query = _normalize_query(_get_value(row, "query", "Query"))
        if query is None:
            continue

        item = aggregated.setdefault(
            query,
            {
                "query": query,
                "impressions": 0,
                "clicks": 0,
                "cost": 0.0,
                "conversions": 0.0,
            },
        )
        item["impressions"] += _to_int(_get_value(row, "impressions", "Impressions"))
        item["clicks"] += _to_int(_get_value(row, "clicks", "Clicks"))
        item["cost"] += _to_float(_get_value(row, "cost", "Cost"))
        item["conversions"] += _to_float(_get_value(row, "conversions", "Conversions"))

    query_items = list(aggregated.values())
    for item in query_items:
        impressions = item["impressions"]
        clicks = item["clicks"]
        cost = item["cost"]
        conversions = item["conversions"]
        item["ctr"] = round((clicks / impressions * 100), 4) if impressions > 0 else 0.0
        item["cpc"] = round((cost / clicks), 4) if clicks > 0 else 0.0
        item["cpa"] = round((cost / conversions), 4) if conversions > 0 else None

    candidates_negative = []
    top_performers = []
    waste_queries = []

    for item in query_items:
        clicks = item["clicks"]
        cost = item["cost"]
        conversions = item["conversions"]

        if conversions > 0:
            top_performers.append(item)

        if clicks > 0 and conversions == 0 and cost > 0:
            waste_queries.append(item)

        is_negative_candidate = clicks >= 10 and conversions == 0
        if target_cpa is not None and target_cpa > 0 and cost >= (2 * target_cpa):
            is_negative_candidate = True
        if is_negative_candidate:
            candidates_negative.append(item)

    candidates_negative.sort(key=lambda item: (item["clicks"], item["cost"]), reverse=True)
    top_performers.sort(key=lambda item: (item["conversions"], item["clicks"], item["cost"]), reverse=True)
    waste_queries.sort(key=lambda item: (item["cost"], item["clicks"]), reverse=True)

    return {
        "total_queries": len(query_items),
        "candidates_negative": [_build_output_item(item) for item in candidates_negative],
        "top_performers": [_build_output_item(item) for item in top_performers],
        "waste_queries": [_build_output_item(item) for item in waste_queries],
    }



IRRELEVANT_NEGATIVE_TOKENS = (
    "костюм",
    "одежда",
    "нос",
    "грим",
    "макияж",
    "аниматор",
    "цирк",
    "раскраска",
    "рисунок",
    "картинки",
    "фото",
    "обои",
    "фильм",
    "актеры",
    "видео",
    "википедия",
    "бесплатно",
    "скачать",
    "wildberries",
    "ozon",
    "валберис",
    "маркет",
    "авито",
)


def _extract_query_tokens(value: str) -> list[str]:
    return re.findall(r"[0-9a-zа-я]+", value.lower())


def build_negative_preview_candidates(rows, analysis: dict, target_cpa: Optional[float] = None):
    negative_queries = {
        item["query"]
        for item in analysis.get("candidates_negative", [])
        if isinstance(item, dict) and isinstance(item.get("query"), str)
    }

    aggregated: dict[str, dict] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue

        query = _normalize_query(_get_value(row, "query", "Query"))
        if query is None:
            continue

        ad_group_id_value = _get_value(row, "ad_group_id", "AdGroupId")
        ad_group_id = ""
        if ad_group_id_value is not None:
            ad_group_id = str(ad_group_id_value).strip()

        item = aggregated.setdefault(
            query,
            {
                "query": query,
                "clicks": 0,
                "cost": 0.0,
                "conversions": 0.0,
                "ad_group_ids": [],
            },
        )
        item["clicks"] += _to_int(_get_value(row, "clicks", "Clicks"))
        item["cost"] += _to_float(_get_value(row, "cost", "Cost"))
        item["conversions"] += _to_float(_get_value(row, "conversions", "Conversions"))
        if ad_group_id and ad_group_id not in item["ad_group_ids"]:
            item["ad_group_ids"].append(ad_group_id)

    candidates = []
    seen = set()

    for query, item in aggregated.items():
        matched_tokens = [token for token in _extract_query_tokens(query) if token in IRRELEVANT_NEGATIVE_TOKENS]
        is_negative_candidate = query in negative_queries

        if not matched_tokens and not is_negative_candidate:
            continue

        suggested_negative = matched_tokens[0] if matched_tokens else query
        ad_group_ids = item["ad_group_ids"]
        if matched_tokens:
            level = "campaign"
            ad_group_id = ""
            if len(matched_tokens) == 1:
                reason = f"Запрос содержит нерелевантный токен '{matched_tokens[0]}'."
            else:
                reason = f"Запрос содержит нерелевантные токены: {', '.join(matched_tokens)}."
        else:
            level = "ad_group" if len(ad_group_ids) == 1 else "campaign"
            ad_group_id = ad_group_ids[0] if level == "ad_group" else ""
            if target_cpa is not None and target_cpa > 0 and item["cost"] >= (2 * target_cpa) and item["conversions"] == 0:
                reason = f"Расход {round(item['cost'], 2)} без конверсий превысил двойной target CPA."
            else:
                reason = "Запрос получил не менее 10 кликов без конверсий."

        signature = (query, suggested_negative, level, ad_group_id)
        if signature in seen:
            continue
        seen.add(signature)

        candidates.append(
            {
                "query": query,
                "suggested_negative": suggested_negative,
                "level": level,
                "reason": reason,
                "ad_group_id": ad_group_id,
                "clicks": item["clicks"],
                "cost": round(item["cost"], 2),
                "conversions": round(item["conversions"], 4),
            }
        )

    candidates.sort(key=lambda item: (item["cost"], item["clicks"], item["query"]), reverse=True)
    return candidates
