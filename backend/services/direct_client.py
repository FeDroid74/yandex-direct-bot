import json
import csv
import io
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from config import settings


class YandexDirectClientError(Exception):
    pass


@dataclass
class YandexDirectResponse:
    status_code: int
    request_id: str
    units: str
    body: Dict[str, Any]


class YandexDirectClient:
    """
    Minimal JSON client for Yandex Direct API.

    Supports:
    - v5 endpoints
    - v501 endpoints (required for Unified Campaign / UPC operations)
    """

    TARGET_SANDBOX = "sandbox"
    TARGET_PRODUCTION = "production"

    DEFAULT_METRICA_COUNTER_ID = 99041859

    DEFAULT_SEARCH_PLACEMENT_TYPES = {
        "SearchResults": "YES",
        "ProductGallery": "YES",
        "DynamicPlaces": "YES",
        "Maps": "NO",
        "SearchOrganizationList": "NO",
    }

    DEFAULT_NETWORK_PLACEMENT_TYPES = {
        "Network": "YES",
        "Maps": "NO",
    }

    def __init__(
        self,
        base_url: Optional[str] = None,
        oauth_token: Optional[str] = None,
        client_login: Optional[str] = None,
        accept_language: Optional[str] = None,
    ) -> None:
        self.base_url = (base_url or settings.yandex_direct_base_url).rstrip("/")
        self.oauth_token = oauth_token or settings.yandex_direct_oauth_token
        self.client_login = client_login or settings.yandex_direct_client_login
        self.accept_language = accept_language or settings.yandex_direct_accept_language

    @classmethod
    def for_target(cls, target: str) -> "YandexDirectClient":
        normalized = str(target).strip().lower()

        if normalized == cls.TARGET_SANDBOX:
            return cls(base_url=settings.yandex_direct_sandbox_base_url)

        if normalized == cls.TARGET_PRODUCTION:
            return cls(base_url=settings.yandex_direct_production_base_url)

        raise YandexDirectClientError(f"unsupported target: {target}")

    def _build_headers(self) -> Dict[str, str]:
        if not self.oauth_token.strip():
            raise YandexDirectClientError("YANDEX_DIRECT_OAUTH_TOKEN is empty")

        headers = {
            "Authorization": f"Bearer {self.oauth_token}",
            "Accept-Language": self.accept_language,
            "Content-Type": "application/json; charset=utf-8",
        }

        if self.client_login.strip():
            headers["Client-Login"] = self.client_login

        return headers

    def _call_with_base_url(
        self,
        base_url: str,
        service: str,
        method: str,
        params: Dict[str, Any],
    ) -> YandexDirectResponse:
        url = f"{base_url.rstrip('/')}/{service}"

        payload = {
            "method": method,
            "params": params,
        }

        request = urllib.request.Request(
            url=url,
            data=json.dumps(payload).encode("utf-8"),
            headers=self._build_headers(),
            method="POST",
        )

        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                raw_body = response.read().decode("utf-8")
                parsed_body = json.loads(raw_body) if raw_body else {}

                return YandexDirectResponse(
                    status_code=response.getcode(),
                    request_id=response.headers.get("RequestId", ""),
                    units=response.headers.get("Units", ""),
                    body=parsed_body,
                )

        except urllib.error.HTTPError as e:
            raw_error = e.read().decode("utf-8", errors="replace")
            try:
                parsed_error = json.loads(raw_error) if raw_error else {}
            except json.JSONDecodeError:
                parsed_error = {"raw_error": raw_error}

            raise YandexDirectClientError(
                f"HTTP {e.code}: {json.dumps(parsed_error, ensure_ascii=False)}"
            ) from e

        except urllib.error.URLError as e:
            raise YandexDirectClientError(f"Connection error: {e.reason}") from e

    def _build_v501_base_url(self) -> str:
        if not self.base_url.endswith("/json/v5"):
            raise YandexDirectClientError(
                f"cannot derive v501 URL from base_url: {self.base_url}"
            )
        return self.base_url[:-len("/json/v5")] + "/json/v501"

    def call_v5(self, service: str, method: str, params: Dict[str, Any]) -> YandexDirectResponse:
        return self._call_with_base_url(
            base_url=self.base_url,
            service=service,
            method=method,
            params=params,
        )

    def call_v501(self, service: str, method: str, params: Dict[str, Any]) -> YandexDirectResponse:
        return self._call_with_base_url(
            base_url=self._build_v501_base_url(),
            service=service,
            method=method,
            params=params,
        )

    def call(self, service: str, method: str, params: Dict[str, Any]) -> YandexDirectResponse:
        return self.call_v5(service, method, params)
    
    def _build_reports_headers(self) -> Dict[str, str]:
        headers = dict(self._build_headers())
        headers.update({
            "processingMode": "auto",
            "returnMoneyInMicros": "false",
            "skipReportHeader": "true",
            "skipColumnHeader": "false",
            "skipReportSummary": "true",
        })
        return headers

    def _build_reports_url(self) -> str:
        return f"{self.base_url.rstrip('/')}/reports"

    def get_campaign_stats_report(
        self,
        campaign_id: int,
        date_from: str,
        date_to: str,
    ) -> Dict[str, Any]:
        payload = {
            "params": {
                "SelectionCriteria": {
                    "DateFrom": date_from,
                    "DateTo": date_to,
                    "Filter": [
                        {
                            "Field": "CampaignId",
                            "Operator": "IN",
                            "Values": [str(campaign_id)],
                        }
                    ],
                },
                "FieldNames": [
                    "Date",
                    "CampaignId",
                    "Clicks",
                    "Impressions",
                    "Cost",
                    "Conversions",
                    "AvgCpc",
                    "ConversionRate",
                    "CostPerConversion",
                ],
                "OrderBy": [
                    {
                        "Field": "Date",
                        "SortOrder": "ASCENDING",
                    }
                ],
                "ReportName": f"campaign-stats-{campaign_id}-{date_from}-{date_to}",
                "ReportType": "CAMPAIGN_PERFORMANCE_REPORT",
                "DateRangeType": "CUSTOM_DATE",
                "Format": "TSV",
                "IncludeVAT": "YES",
                "IncludeDiscount": "YES",
            }
        }

        request = urllib.request.Request(
            url=self._build_reports_url(),
            data=json.dumps(payload).encode("utf-8"),
            headers=self._build_reports_headers(),
            method="POST",
        )

        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                status_code = response.getcode()
                raw_tsv = response.read().decode("utf-8")

                result: Dict[str, Any] = {
                    "status_code": status_code,
                    "request_id": response.headers.get("RequestId", ""),
                    "units": response.headers.get("Units", ""),
                    "retry_in": response.headers.get("retryIn", ""),
                }

                if status_code in (201, 202):
                    result["report_status"] = "processing"
                    result["rows"] = []
                    return result

                rows = list(csv.DictReader(io.StringIO(raw_tsv), delimiter="\t"))

                result["report_status"] = "ready"
                result["rows"] = rows
                result["raw_tsv"] = raw_tsv
                return result

        except urllib.error.HTTPError as e:
            raw_error = e.read().decode("utf-8", errors="replace")
            try:
                parsed_error = json.loads(raw_error) if raw_error else {}
            except json.JSONDecodeError:
                parsed_error = {"raw_error": raw_error}

            raise YandexDirectClientError(
                f"HTTP {e.code}: {json.dumps(parsed_error, ensure_ascii=False)}"
            ) from e

        except urllib.error.URLError as e:
            raise YandexDirectClientError(f"Connection error: {e.reason}") from e
        
    def get_campaign_goal_stats_report(
        self,
        campaign_id: int,
        date_from: str,
        date_to: str,
        goal_id: int,
    ) -> Dict[str, Any]:
        payload = {
            "params": {
                "SelectionCriteria": {
                    "DateFrom": date_from,
                    "DateTo": date_to,
                    "Filter": [
                        {
                            "Field": "CampaignId",
                            "Operator": "IN",
                            "Values": [str(campaign_id)],
                        }
                    ],
                },
                "Goals": [str(goal_id)],
                "AttributionModels": ["LC"],
                "FieldNames": [
                    "Date",
                    "CampaignId",
                    "Conversions",
                ],
                "ReportName": f"campaign-goal-stats-{campaign_id}-{goal_id}",
                "ReportType": "CAMPAIGN_PERFORMANCE_REPORT",
                "DateRangeType": "CUSTOM_DATE",
                "Format": "TSV",
                "IncludeVAT": "YES",
                "IncludeDiscount": "YES",
            }
        }

        request = urllib.request.Request(
            url=self._build_reports_url(),
            data=json.dumps(payload).encode("utf-8"),
            headers=self._build_reports_headers(),
            method="POST",
        )

        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                status_code = response.getcode()
                raw_tsv = response.read().decode("utf-8")

                result: Dict[str, Any] = {
                    "status_code": status_code,
                    "request_id": response.headers.get("RequestId", ""),
                    "units": response.headers.get("Units", ""),
                    "retry_in": response.headers.get("retryIn", ""),
                }

                if status_code in (201, 202):
                    result["report_status"] = "processing"
                    result["rows"] = []
                    return result

                rows = list(csv.DictReader(io.StringIO(raw_tsv), delimiter="\t"))

                result["report_status"] = "ready"
                result["rows"] = rows
                result["raw_tsv"] = raw_tsv
                return result

        except urllib.error.HTTPError as e:
            raw_error = e.read().decode("utf-8", errors="replace")
            try:
                parsed_error = json.loads(raw_error) if raw_error else {}
            except json.JSONDecodeError:
                parsed_error = {"raw_error": raw_error}

            raise YandexDirectClientError(
                f"HTTP {e.code}: {json.dumps(parsed_error, ensure_ascii=False)}"
            ) from e

        except urllib.error.URLError as e:
            raise YandexDirectClientError(f"Connection error: {e.reason}") from e

    def ping(self) -> Dict[str, Any]:
        return {
            "configured": bool(self.oauth_token.strip()),
            "base_url": self.base_url,
            "has_client_login": bool(self.client_login.strip()),
            "accept_language": self.accept_language,
        }

    def get_campaigns(self) -> Dict[str, Any]:
        response = self.call_v5(
            service="campaigns",
            method="get",
            params={
                "SelectionCriteria": {},
                "FieldNames": ["Id", "Name", "State"],
            },
        )
        return response.body

    def get_campaign_details(self, campaign_id: int) -> Dict[str, Any]:
        response = self.call_v501(
            service="campaigns",
            method="get",
            params={
                "SelectionCriteria": {
                    "Ids": [campaign_id],
                },
                "FieldNames": [
                    "Id",
                    "Name",
                    "Type",
                    "State",
                    "Status",
                    "StatusPayment",
                    "StartDate",
                    "TimeZone",
                    "TimeTargeting",
                ],
                "UnifiedCampaignFieldNames": [
                    "BiddingStrategy",
                    "CounterIds",
                    "Settings",
                ],
                "UnifiedCampaignSearchStrategyPlacementTypesFieldNames": [
                    "SearchResults",
                    "ProductGallery",
                    "DynamicPlaces",
                    "Maps",
                    "SearchOrganizationList",
                ],
            },
        )
        return response.body

    def update_campaign(
        self,
        campaign_id: int,
        name: Optional[str] = None,
        goal_id: Optional[int] = None,
        cpa_micros: Optional[int] = None,
        weekly_budget_micros: Optional[int] = None,
    ) -> Dict[str, Any]:
        campaign_item: Dict[str, Any] = {
            "Id": campaign_id,
        }

        if name is not None:
            campaign_item["Name"] = name

        strategy_update_requested = any(
            value is not None for value in (goal_id, cpa_micros, weekly_budget_micros)
        )

        if strategy_update_requested:
            if goal_id is None or cpa_micros is None or weekly_budget_micros is None:
                raise YandexDirectClientError(
                    "goal_id, cpa_micros, and weekly_budget_micros must all be provided for strategy update"
                )

            campaign_item["UnifiedCampaign"] = {
                "BiddingStrategy": {
                    "Search": {
                        "BiddingStrategyType": "PAY_FOR_CONVERSION",
                        "PayForConversion": {
                            "GoalId": goal_id,
                            "Cpa": cpa_micros,
                            "WeeklySpendLimit": weekly_budget_micros,
                        },
                    },
                    "Network": {
                        "BiddingStrategyType": "NETWORK_DEFAULT",
                    },
                }
            }

        response = self.call_v501(
            service="campaigns",
            method="update",
            params={
                "Campaigns": [campaign_item],
            },
        )
        return response.body

    def update_campaign_sandbox(
        self,
        campaign_id: int,
        name: Optional[str] = None,
        goal_id: Optional[int] = None,
        cpa_micros: Optional[int] = None,
        weekly_budget_micros: Optional[int] = None,
    ) -> Dict[str, Any]:
        return self.update_campaign(
            campaign_id=campaign_id,
            name=name,
            goal_id=goal_id,
            cpa_micros=cpa_micros,
            weekly_budget_micros=weekly_budget_micros,
        )

    def update_campaign_production(
        self,
        campaign_id: int,
        name: Optional[str] = None,
        goal_id: Optional[int] = None,
        cpa_micros: Optional[int] = None,
        weekly_budget_micros: Optional[int] = None,
    ) -> Dict[str, Any]:
        return self.update_campaign(
            campaign_id=campaign_id,
            name=name,
            goal_id=goal_id,
            cpa_micros=cpa_micros,
            weekly_budget_micros=weekly_budget_micros,
        )

    def get_ad_group_details(self, ad_group_id: int) -> Dict[str, Any]:
        response = self.call_v501(
            service="adgroups",
            method="get",
            params={
                "SelectionCriteria": {
                    "Ids": [ad_group_id],
                },
                "FieldNames": [
                    "Id",
                    "Name",
                    "CampaignId",
                    "RegionIds",
                    "Status",
                    "ServingStatus",
                    "Type",
                ],
            },
        )
        return response.body

    def get_ad_details(self, ad_id: int) -> Dict[str, Any]:
        response = self.call_v501(
            service="ads",
            method="get",
            params={
                "SelectionCriteria": {
                    "Ids": [ad_id],
                },
                "FieldNames": [
                    "Id",
                    "CampaignId",
                    "AdGroupId",
                    "Status",
                    "State",
                    "StatusClarification",
                    "Type",
                ],
                "TextAdFieldNames": [
                    "Title",
                    "Text",
                    "Href",
                    "DisplayUrlPath",
                ],
            },
        )
        return response.body

    def add_unified_campaign(
        self,
        name: str,
        start_date: str,
        goal_id: int,
        cpa_micros: int,
        weekly_budget_micros: int,
        counter_id: int = DEFAULT_METRICA_COUNTER_ID,
    ) -> Dict[str, Any]:
        response = self.call_v501(
            service="campaigns",
            method="add",
            params={
                "Campaigns": [
                    {
                        "Name": name,
                        "StartDate": start_date,
                        "TimeZone": "Europe/Moscow",
                        "UnifiedCampaign": {
                            "CounterIds": {
                                "Items": [counter_id],
                            },
                            "BiddingStrategy": {
                                "Search": {
                                    "BiddingStrategyType": "PAY_FOR_CONVERSION",
                                    "PlacementTypes": dict(self.DEFAULT_SEARCH_PLACEMENT_TYPES),
                                    "PayForConversion": {
                                        "GoalId": goal_id,
                                        "Cpa": cpa_micros,
                                        "WeeklySpendLimit": weekly_budget_micros,
                                    },
                                },
                                "Network": {
                                    "BiddingStrategyType": "NETWORK_DEFAULT",
                                    "PlacementTypes": dict(self.DEFAULT_NETWORK_PLACEMENT_TYPES),
                                },
                            },
                        },
                    }
                ]
            },
        )
        return response.body

    def add_unified_campaign_sandbox(
        self,
        name: str,
        start_date: str,
        goal_id: int,
        cpa_micros: int,
        weekly_budget_micros: int,
        counter_id: int = DEFAULT_METRICA_COUNTER_ID,
    ) -> Dict[str, Any]:
        return self.add_unified_campaign(
            name=name,
            start_date=start_date,
            goal_id=goal_id,
            cpa_micros=cpa_micros,
            weekly_budget_micros=weekly_budget_micros,
            counter_id=counter_id,
        )

    def add_unified_campaign_production(
        self,
        name: str,
        start_date: str,
        goal_id: int,
        cpa_micros: int,
        weekly_budget_micros: int,
        counter_id: int = DEFAULT_METRICA_COUNTER_ID,
    ) -> Dict[str, Any]:
        return self.add_unified_campaign(
            name=name,
            start_date=start_date,
            goal_id=goal_id,
            cpa_micros=cpa_micros,
            weekly_budget_micros=weekly_budget_micros,
            counter_id=counter_id,
        )

    def add_unified_ad_group(
        self,
        campaign_id: int,
        name: str,
        region_ids: List[int],
        offer_retargeting: str = "NO",
        negative_keywords: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        ad_group_item: Dict[str, Any] = {
            "Name": name,
            "CampaignId": campaign_id,
            "RegionIds": region_ids,
            "UnifiedAdGroup": {
                "OfferRetargeting": offer_retargeting,
            },
        }

        if negative_keywords:
            ad_group_item["NegativeKeywords"] = {
                "Items": negative_keywords,
            }

        response = self.call_v501(
            service="adgroups",
            method="add",
            params={
                "AdGroups": [ad_group_item],
            },
        )
        return response.body

    def add_unified_ad_group_sandbox(
        self,
        campaign_id: int,
        name: str,
        region_ids: List[int],
        offer_retargeting: str = "NO",
        negative_keywords: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        return self.add_unified_ad_group(
            campaign_id=campaign_id,
            name=name,
            region_ids=region_ids,
            offer_retargeting=offer_retargeting,
            negative_keywords=negative_keywords,
        )

    def add_unified_ad_group_production(
        self,
        campaign_id: int,
        name: str,
        region_ids: List[int],
        offer_retargeting: str = "NO",
        negative_keywords: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        return self.add_unified_ad_group(
            campaign_id=campaign_id,
            name=name,
            region_ids=region_ids,
            offer_retargeting=offer_retargeting,
            negative_keywords=negative_keywords,
        )

    def add_text_ad(
        self,
        ad_group_id: int,
        title: str,
        text: str,
        href: str,
        display_url_path: Optional[str] = None,
    ) -> Dict[str, Any]:
        ad_item: Dict[str, Any] = {
            "AdGroupId": ad_group_id,
            "TextAd": {
                "Title": title,
                "Text": text,
                "Href": href,
            },
        }

        if display_url_path:
            ad_item["TextAd"]["DisplayUrlPath"] = display_url_path

        response = self.call_v501(
            service="ads",
            method="add",
            params={
                "Ads": [ad_item],
            },
        )
        return response.body

    def add_text_ad_sandbox(
        self,
        ad_group_id: int,
        title: str,
        text: str,
        href: str,
        display_url_path: Optional[str] = None,
    ) -> Dict[str, Any]:
        return self.add_text_ad(
            ad_group_id=ad_group_id,
            title=title,
            text=text,
            href=href,
            display_url_path=display_url_path,
        )

    def add_text_ad_production(
        self,
        ad_group_id: int,
        title: str,
        text: str,
        href: str,
        display_url_path: Optional[str] = None,
    ) -> Dict[str, Any]:
        return self.add_text_ad(
            ad_group_id=ad_group_id,
            title=title,
            text=text,
            href=href,
            display_url_path=display_url_path,
        )

    def moderate_ads(self, ad_ids: List[int]) -> Dict[str, Any]:
        response = self.call_v501(
            service="ads",
            method="moderate",
            params={
                "SelectionCriteria": {
                    "Ids": ad_ids,
                }
            },
        )
        return response.body

    def moderate_ads_sandbox(self, ad_ids: List[int]) -> Dict[str, Any]:
        return self.moderate_ads(ad_ids=ad_ids)

    def moderate_ads_production(self, ad_ids: List[int]) -> Dict[str, Any]:
        return self.moderate_ads(ad_ids=ad_ids)