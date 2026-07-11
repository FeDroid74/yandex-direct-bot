import json
import os
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional

from config import settings


class YandexMetrikaClientError(Exception):
    pass


class YandexMetrikaClient:
    BASE_URL = "https://api-metrika.yandex.net"

    ATTRIBUTION_ALIASES = {
        "lastsign": "lastsign",
        "last_significant": "lastsign",
        "significant": "lastsign",
        "last": "last",
    }

    def __init__(
        self,
        oauth_token: Optional[str] = None,
        base_url: Optional[str] = None,
        accept_language: Optional[str] = None,
    ) -> None:
        self.base_url = (base_url or self.BASE_URL).rstrip("/")
        self.oauth_token = (
            oauth_token
            or os.getenv("YANDEX_METRIKA_OAUTH_TOKEN", "")
            or settings.yandex_direct_oauth_token
        )
        self.accept_language = accept_language or settings.yandex_direct_accept_language or "ru"

    @classmethod
    def normalize_attribution(cls, attribution: Optional[str]) -> str:
        if attribution is None:
            return "lastsign"
        normalized = str(attribution).strip().lower().replace("-", "_")
        return cls.ATTRIBUTION_ALIASES.get(normalized, "")

    def _build_headers(self) -> Dict[str, str]:
        if not self.oauth_token.strip():
            raise YandexMetrikaClientError("YANDEX_METRIKA_OAUTH_TOKEN or YANDEX_DIRECT_OAUTH_TOKEN is required")

        return {
            "Authorization": f"OAuth {self.oauth_token}",
            "Accept": "application/json",
            "Accept-Language": self.accept_language,
            "User-Agent": "OpenClawYandexDirectBot/1.0",
        }

    def _get_json(self, path: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        query = urllib.parse.urlencode(params or {}, doseq=True)
        url = f"{self.base_url}{path}"
        if query:
            url = f"{url}?{query}"

        request = urllib.request.Request(
            url=url,
            headers=self._build_headers(),
            method="GET",
        )

        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                raw_body = response.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as e:
            raw_error = e.read().decode("utf-8", errors="replace")
            try:
                parsed_error = json.loads(raw_error) if raw_error else {}
            except json.JSONDecodeError:
                parsed_error = {"raw_error": raw_error}
            raise YandexMetrikaClientError(
                f"HTTP {e.code}: {json.dumps(parsed_error, ensure_ascii=False)}"
            ) from e
        except urllib.error.URLError as e:
            raise YandexMetrikaClientError(f"Connection error: {e.reason}") from e

        try:
            payload = json.loads(raw_body) if raw_body else {}
        except json.JSONDecodeError as e:
            raise YandexMetrikaClientError("Metrika API returned non-JSON response") from e

        if not isinstance(payload, dict):
            raise YandexMetrikaClientError("Metrika API returned unexpected JSON payload")
        return payload

    def ping(self) -> Dict[str, Any]:
        return {
            "configured": bool(self.oauth_token.strip()),
            "base_url": self.base_url,
            "token_source": "YANDEX_METRIKA_OAUTH_TOKEN"
            if os.getenv("YANDEX_METRIKA_OAUTH_TOKEN", "").strip()
            else "YANDEX_DIRECT_OAUTH_TOKEN",
        }

    def get_counter(self, counter_id: int) -> Dict[str, Any]:
        return self._get_json(f"/management/v1/counter/{counter_id}")

    def get_goals(self, counter_id: int) -> Dict[str, Any]:
        return self._get_json(f"/management/v1/counter/{counter_id}/goals")

    def get_report(
        self,
        *,
        counter_id: int,
        metrics: List[str],
        dimensions: Optional[List[str]] = None,
        date_from: str,
        date_to: str,
        filters: Optional[str] = None,
        limit: int = 100,
        sort: Optional[str] = None,
    ) -> Dict[str, Any]:
        params: Dict[str, Any] = {
            "ids": str(counter_id),
            "metrics": ",".join(metrics),
            "date1": date_from,
            "date2": date_to,
            "limit": str(limit),
            "accuracy": "full",
            "lang": self.accept_language,
        }
        if dimensions:
            params["dimensions"] = ",".join(dimensions)
        if filters:
            params["filters"] = filters
        if sort:
            params["sort"] = sort

        return self._get_json("/stat/v1/data", params=params)

    def get_direct_phrase_quality_report(
        self,
        *,
        counter_id: int,
        campaign_id: int,
        date_from: str,
        date_to: str,
        goal_id: int,
        attribution: str = "lastsign",
        dimension_kind: str = "direct_search_phrase",
        limit: int = 100,
    ) -> Dict[str, Any]:
        normalized_attribution = self.normalize_attribution(attribution)
        if not normalized_attribution:
            raise YandexMetrikaClientError("attribution must be 'lastsign' or 'last'")

        if dimension_kind == "utm_term":
            dimension = f"ym:s:{normalized_attribution}UTMTerm"
        elif dimension_kind == "direct_phrase_or_condition":
            dimension = f"ym:s:{normalized_attribution}DirectPhraseOrCond"
        else:
            dimension = f"ym:s:{normalized_attribution}DirectSearchPhrase"

        direct_campaign_dimension = f"ym:s:{normalized_attribution}DirectClickOrder"
        filters = f"{direct_campaign_dimension}=='{campaign_id}'"
        metrics = [
            "ym:s:visits",
            "ym:s:users",
            "ym:s:bounceRate",
            "ym:s:pageDepth",
            "ym:s:avgVisitDurationSeconds",
            f"ym:s:goal{goal_id}reaches",
            f"ym:s:goal{goal_id}conversionRate",
        ]

        return self.get_report(
            counter_id=counter_id,
            metrics=metrics,
            dimensions=[dimension],
            date_from=date_from,
            date_to=date_to,
            filters=filters,
            limit=limit,
            sort="-ym:s:visits",
        )
