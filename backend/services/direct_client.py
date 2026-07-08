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

    PLACEMENT_BOTH = "both"
    PLACEMENT_SEARCH_ONLY = "search_only"
    PLACEMENT_NETWORK_ONLY = "network_only"

    PLACEMENT_ALIASES = {
        "both": PLACEMENT_BOTH,
        "all": PLACEMENT_BOTH,
        "search": PLACEMENT_SEARCH_ONLY,
        "search_only": PLACEMENT_SEARCH_ONLY,
        "poisk": PLACEMENT_SEARCH_ONLY,
        "network": PLACEMENT_NETWORK_ONLY,
        "network_only": PLACEMENT_NETWORK_ONLY,
        "rsya": PLACEMENT_NETWORK_ONLY,
        "rsy": PLACEMENT_NETWORK_ONLY,
        "рся": PLACEMENT_NETWORK_ONLY,
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

    @classmethod
    def normalize_placement_type(cls, placement_type: Optional[str]) -> str:
        if placement_type is None:
            return cls.PLACEMENT_BOTH

        normalized = str(placement_type).strip().lower().replace("-", "_")
        if normalized in cls.PLACEMENT_ALIASES:
            return cls.PLACEMENT_ALIASES[normalized]

        raise YandexDirectClientError(
            "placement_type must be 'both', 'search_only', or 'network_only'"
        )

    @classmethod
    def infer_unified_campaign_placement_type(cls, unified_campaign: Dict[str, Any]) -> str:
        bidding_strategy = unified_campaign.get("BiddingStrategy", {})
        search_strategy = bidding_strategy.get("Search", {})
        network_strategy = bidding_strategy.get("Network", {})

        search_type = str(search_strategy.get("BiddingStrategyType", "")).upper()
        network_type = str(network_strategy.get("BiddingStrategyType", "")).upper()

        if search_type == "SERVING_OFF" and network_type != "SERVING_OFF":
            return cls.PLACEMENT_NETWORK_ONLY

        if network_type == "SERVING_OFF" and search_type != "SERVING_OFF":
            return cls.PLACEMENT_SEARCH_ONLY

        search_places = search_strategy.get("PlacementTypes", {})
        network_places = network_strategy.get("PlacementTypes", {})
        search_enabled = any(value == "YES" for value in search_places.values())
        network_enabled = any(value == "YES" for value in network_places.values())

        if search_enabled and not network_enabled:
            return cls.PLACEMENT_SEARCH_ONLY

        if network_enabled and not search_enabled:
            return cls.PLACEMENT_NETWORK_ONLY

        return cls.PLACEMENT_BOTH

    def build_unified_bidding_strategy(
        self,
        *,
        goal_id: int,
        cpa_micros: int,
        weekly_budget_micros: int,
        placement_type: Optional[str] = None,
    ) -> Dict[str, Any]:
        normalized_placement = self.normalize_placement_type(placement_type)
        pay_for_conversion = {
            "GoalId": goal_id,
            "Cpa": cpa_micros,
            "WeeklySpendLimit": weekly_budget_micros,
        }

        search_pay_for_conversion = {
            "BiddingStrategyType": "PAY_FOR_CONVERSION",
            "PlacementTypes": dict(self.DEFAULT_SEARCH_PLACEMENT_TYPES),
            "PayForConversion": dict(pay_for_conversion),
        }
        network_default = {
            "BiddingStrategyType": "NETWORK_DEFAULT",
            "PlacementTypes": dict(self.DEFAULT_NETWORK_PLACEMENT_TYPES),
        }

        if normalized_placement == self.PLACEMENT_SEARCH_ONLY:
            return {
                "Search": search_pay_for_conversion,
                "Network": {
                    "BiddingStrategyType": "SERVING_OFF",
                },
            }

        if normalized_placement == self.PLACEMENT_NETWORK_ONLY:
            return {
                "Search": {
                    "BiddingStrategyType": "SERVING_OFF",
                },
                "Network": {
                    "BiddingStrategyType": "PAY_FOR_CONVERSION",
                    "PlacementTypes": dict(self.DEFAULT_NETWORK_PLACEMENT_TYPES),
                    "PayForConversion": dict(pay_for_conversion),
                },
            }

        return {
            "Search": search_pay_for_conversion,
            "Network": network_default,
        }

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


    def get_search_terms_report(
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
                    "AdGroupId",
                    "Query",
                    "Criteria",
                    "CriteriaType",
                    "Impressions",
                    "Clicks",
                    "Cost",
                    "AvgCpc",
                    "Conversions",
                ],
                "ReportName": f"search-terms-{campaign_id}-{date_from}-{date_to}",
                "ReportType": "SEARCH_QUERY_PERFORMANCE_REPORT",
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

                rows = list(csv.DictReader(io.StringIO(raw_tsv), delimiter="	"))

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
                    "TrackingParams",
                    "NegativeKeywordSharedSetIds",
                ],
                "UnifiedCampaignSearchStrategyPlacementTypesFieldNames": [
                    "SearchResults",
                    "ProductGallery",
                    "DynamicPlaces",
                    "Maps",
                    "SearchOrganizationList",
                ],
                "UnifiedCampaignNetworkStrategyPlacementTypesFieldNames": [
                    "Network",
                    "Maps",
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
        placement_type: Optional[str] = None,
    ) -> Dict[str, Any]:
        campaign_item: Dict[str, Any] = {
            "Id": campaign_id,
        }

        if name is not None:
            campaign_item["Name"] = name

        strategy_update_requested = any(
            value is not None for value in (goal_id, cpa_micros, weekly_budget_micros, placement_type)
        )

        if strategy_update_requested:
            if goal_id is None or cpa_micros is None or weekly_budget_micros is None:
                raise YandexDirectClientError(
                    "goal_id, cpa_micros, and weekly_budget_micros must all be provided for strategy update"
                )

            campaign_item["UnifiedCampaign"] = {
                "BiddingStrategy": self.build_unified_bidding_strategy(
                    goal_id=goal_id,
                    cpa_micros=cpa_micros,
                    weekly_budget_micros=weekly_budget_micros,
                    placement_type=placement_type,
                )
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
        placement_type: Optional[str] = None,
    ) -> Dict[str, Any]:
        return self.update_campaign(
            campaign_id=campaign_id,
            name=name,
            goal_id=goal_id,
            cpa_micros=cpa_micros,
            weekly_budget_micros=weekly_budget_micros,
            placement_type=placement_type,
        )

    def update_campaign_production(
        self,
        campaign_id: int,
        name: Optional[str] = None,
        goal_id: Optional[int] = None,
        cpa_micros: Optional[int] = None,
        weekly_budget_micros: Optional[int] = None,
        placement_type: Optional[str] = None,
    ) -> Dict[str, Any]:
        return self.update_campaign(
            campaign_id=campaign_id,
            name=name,
            goal_id=goal_id,
            cpa_micros=cpa_micros,
            weekly_budget_micros=weekly_budget_micros,
            placement_type=placement_type,
        )

    def update_campaign_negative_keyword_shared_set_ids(
        self,
        campaign_id: int,
        shared_set_ids: List[int],
    ) -> Dict[str, Any]:
        response = self.call_v501(
            service="campaigns",
            method="update",
            params={
                "Campaigns": [
                    {
                        "Id": campaign_id,
                        "UnifiedCampaign": {
                            "NegativeKeywordSharedSetIds": {
                                "Items": shared_set_ids,
                            }
                        },
                    }
                ]
            },
        )
        return response.body

    def update_campaign_tracking_params(
        self,
        campaign_id: int,
        tracking_params: str,
        counter_ids: Optional[List[int]] = None,
        negative_keyword_shared_set_ids: Optional[List[int]] = None,
    ) -> Dict[str, Any]:
        unified_campaign: Dict[str, Any] = {
            "TrackingParams": tracking_params,
        }

        if counter_ids is not None:
            unified_campaign["CounterIds"] = {
                "Items": counter_ids,
            }

        if negative_keyword_shared_set_ids is not None:
            unified_campaign["NegativeKeywordSharedSetIds"] = {
                "Items": negative_keyword_shared_set_ids,
            }

        response = self.call_v501(
            service="campaigns",
            method="update",
            params={
                "Campaigns": [
                    {
                        "Id": campaign_id,
                        "UnifiedCampaign": unified_campaign,
                    }
                ]
            },
        )
        return response.body

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
                    "NegativeKeywords",
                    "Status",
                    "ServingStatus",
                    "Type",
                ],
            },
        )
        return response.body

    def get_autotargeting_keywords(self, ad_group_id: int) -> Dict[str, Any]:
        response = self.call_v501(
            service="keywords",
            method="get",
            params={
                "SelectionCriteria": {
                    "AdGroupIds": [ad_group_id],
                },
                "FieldNames": [
                    "Id",
                    "Keyword",
                    "State",
                    "Status",
                    "ServingStatus",
                    "AdGroupId",
                    "CampaignId",
                    "Bid",
                    "AutotargetingSearchBidIsAuto",
                    "ContextBid",
                    "StrategyPriority",
                ],
                "AutotargetingSettingsCategoriesFieldNames": [
                    "Exact",
                    "Narrow",
                    "Alternative",
                    "Accessory",
                    "Broader",
                ],
                "AutotargetingSettingsBrandOptionsFieldNames": [
                    "WithoutBrands",
                    "WithAdvertiserBrand",
                    "WithCompetitorsBrand",
                ],
            },
        )
        return response.body


    def get_keywords(
        self,
        *,
        ad_group_ids: Optional[List[int]] = None,
        campaign_ids: Optional[List[int]] = None,
        ids: Optional[List[int]] = None,
        field_names: Optional[List[str]] = None,
        include_autotargeting_settings: bool = False,
    ) -> Dict[str, Any]:
        selection_criteria: Dict[str, Any] = {}
        if ids:
            selection_criteria["Ids"] = ids
        if ad_group_ids:
            selection_criteria["AdGroupIds"] = ad_group_ids
        if campaign_ids:
            selection_criteria["CampaignIds"] = campaign_ids

        if not selection_criteria:
            raise YandexDirectClientError("keywords.get requires ids, ad_group_ids, or campaign_ids")

        params: Dict[str, Any] = {
            "SelectionCriteria": selection_criteria,
            "FieldNames": field_names or [
                "Id",
                "Keyword",
                "State",
                "Status",
                "ServingStatus",
                "AdGroupId",
                "CampaignId",
            ],
        }

        if include_autotargeting_settings:
            params["AutotargetingSettingsCategoriesFieldNames"] = [
                "Exact",
                "Narrow",
                "Alternative",
                "Accessory",
                "Broader",
            ]
            params["AutotargetingSettingsBrandOptionsFieldNames"] = [
                "WithoutBrands",
                "WithAdvertiserBrand",
                "WithCompetitorsBrand",
            ]

        response = self.call_v501(
            service="keywords",
            method="get",
            params=params,
        )
        return response.body

    def add_keywords(
        self,
        keywords: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        response = self.call_v501(
            service="keywords",
            method="add",
            params={
                "Keywords": keywords,
            },
        )
        return response.body

    def update_autotargeting_settings(
        self,
        autotargeting_id: int,
        autotargeting_settings: Dict[str, Any],
    ) -> Dict[str, Any]:
        response = self.call_v501(
            service="keywords",
            method="update",
            params={
                "Keywords": [
                    {
                        "Id": autotargeting_id,
                        "AutotargetingSettings": autotargeting_settings,
                    }
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
                    "AdImageHash",
                    "AdImageModeration",
                    "SitelinkSetId",
                    "AdExtensions",
                ],
            },
        )
        return response.body


    def list_ad_groups(self, campaign_id: int) -> Dict[str, Any]:
        response = self.call_v501(
            service="adgroups",
            method="get",
            params={
                "SelectionCriteria": {
                    "CampaignIds": [campaign_id],
                },
                "FieldNames": [
                    "Id",
                    "Name",
                    "CampaignId",
                    "RegionIds",
                    "NegativeKeywords",
                    "Status",
                    "ServingStatus",
                    "Type",
                ],
            },
        )
        return response.body

    def update_ad_group_negative_keywords(
        self,
        ad_group_id: int,
        negative_keywords: List[str],
    ) -> Dict[str, Any]:
        response = self.call_v501(
            service="adgroups",
            method="update",
            params={
                "AdGroups": [
                    {
                        "Id": ad_group_id,
                        "NegativeKeywords": {
                            "Items": negative_keywords,
                        },
                    }
                ],
            },
        )
        return response.body

    def list_ads(self, ad_group_id: int) -> Dict[str, Any]:
        response = self.call_v501(
            service="ads",
            method="get",
            params={
                "SelectionCriteria": {
                    "AdGroupIds": [ad_group_id],
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
                    "AdImageHash",
                    "AdImageModeration",
                    "SitelinkSetId",
                    "AdExtensions",
                ],
            },
        )
        return response.body

    def get_callouts(
        self,
        ids: Optional[List[int]] = None,
    ) -> Dict[str, Any]:
        selection_criteria: Dict[str, Any] = {
            "Types": ["CALLOUT"],
        }
        if ids:
            selection_criteria["Ids"] = ids

        response = self.call_v501(
            service="adextensions",
            method="get",
            params={
                "SelectionCriteria": selection_criteria,
                "FieldNames": [
                    "Id",
                    "Type",
                    "Status",
                    "StatusClarification",
                    "Associated",
                    "State",
                ],
                "CalloutFieldNames": [
                    "CalloutText",
                ],
            },
        )
        return response.body

    def add_callouts(
        self,
        callout_texts: List[str],
    ) -> Dict[str, Any]:
        response = self.call_v501(
            service="adextensions",
            method="add",
            params={
                "AdExtensions": [
                    {
                        "Callout": {
                            "CalloutText": callout_text,
                        }
                    }
                    for callout_text in callout_texts
                ],
            },
        )
        return response.body

    def add_sitelinks(
        self,
        sitelinks: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        response = self.call_v501(
            service="sitelinks",
            method="add",
            params={
                "SitelinksSets": [
                    {
                        "Sitelinks": sitelinks,
                    }
                ],
            },
        )
        return response.body

    def add_ad_image(
        self,
        name: str,
        image_data_base64: str,
    ) -> Dict[str, Any]:
        response = self.call_v501(
            service="adimages",
            method="add",
            params={
                "AdImages": [
                    {
                        "ImageData": image_data_base64,
                        "Name": name,
                    }
                ],
            },
        )
        return response.body

    def get_ad_images(
        self,
        ad_image_hashes: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        params: Dict[str, Any] = {
            "FieldNames": [
                "AdImageHash",
                "Name",
                "Associated",
                "Type",
                "Subtype",
                "OriginalUrl",
                "PreviewUrl",
            ],
        }

        if ad_image_hashes is not None:
            params["SelectionCriteria"] = {
                "AdImageHashes": ad_image_hashes,
            }

        response = self.call_v501(
            service="adimages",
            method="get",
            params=params,
        )
        return response.body

    def get_sitelinks(
        self,
        ids: Optional[List[int]] = None,
        sitelink_field_names: Optional[List[str]] = None,
        limit: Optional[int] = None,
        offset: Optional[int] = None,
    ) -> Dict[str, Any]:
        params: Dict[str, Any] = {}

        if ids is not None:
            params["SelectionCriteria"] = {
                "Ids": ids,
            }

        if sitelink_field_names:
            params["FieldNames"] = ["Id"]
            params["SitelinkFieldNames"] = sitelink_field_names
        else:
            params["FieldNames"] = ["Id", "Sitelinks"]

        if limit is not None or offset is not None:
            page: Dict[str, int] = {}
            if limit is not None:
                page["Limit"] = limit
            if offset is not None:
                page["Offset"] = offset
            params["Page"] = page

        response = self.call_v501(
            service="sitelinks",
            method="get",
            params=params,
        )
        return response.body

    def add_negative_keyword_shared_set(
        self,
        name: str,
        negative_keywords: List[str],
    ) -> Dict[str, Any]:
        response = self.call_v501(
            service="negativekeywordsharedsets",
            method="add",
            params={
                "NegativeKeywordSharedSets": [
                    {
                        "Name": name,
                        "NegativeKeywords": negative_keywords,
                    }
                ],
            },
        )
        return response.body

    def get_negative_keyword_shared_sets(
        self,
        ids: Optional[List[int]] = None,
        field_names: Optional[List[str]] = None,
        limit: Optional[int] = None,
        offset: Optional[int] = None,
    ) -> Dict[str, Any]:
        params: Dict[str, Any] = {
            "FieldNames": field_names or ["Id", "Name", "NegativeKeywords", "Associated"],
        }

        if ids is not None:
            params["SelectionCriteria"] = {
                "Ids": ids,
            }

        if limit is not None or offset is not None:
            page: Dict[str, int] = {}
            if limit is not None:
                page["Limit"] = limit
            if offset is not None:
                page["Offset"] = offset
            params["Page"] = page

        response = self.call_v501(
            service="negativekeywordsharedsets",
            method="get",
            params=params,
        )
        return response.body


    def update_negative_keyword_shared_set(
        self,
        shared_set_id: int,
        *,
        name: Optional[str] = None,
        negative_keywords: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        item: Dict[str, Any] = {
            "Id": shared_set_id,
        }
        if name is not None:
            item["Name"] = name
        if negative_keywords is not None:
            item["NegativeKeywords"] = negative_keywords

        response = self.call_v501(
            service="negativekeywordsharedsets",
            method="update",
            params={
                "NegativeKeywordSharedSets": [item],
            },
        )
        return response.body

    def suspend_ads(self, ad_ids: List[int]) -> Dict[str, Any]:
        response = self.call_v501(
            service="ads",
            method="suspend",
            params={
                "SelectionCriteria": {
                    "Ids": ad_ids,
                },
            },
        )
        return response.body

    def delete_ads(self, ad_ids: List[int]) -> Dict[str, Any]:
        response = self.call_v501(
            service="ads",
            method="delete",
            params={
                "SelectionCriteria": {
                    "Ids": ad_ids,
                },
            },
        )
        return response.body

    def delete_ad_groups(self, ad_group_ids: List[int]) -> Dict[str, Any]:
        response = self.call_v501(
            service="adgroups",
            method="delete",
            params={
                "SelectionCriteria": {
                    "Ids": ad_group_ids,
                },
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
        tracking_params: Optional[str] = None,
        placement_type: Optional[str] = None,
    ) -> Dict[str, Any]:
        campaign_item: Dict[str, Any] = {
            "Name": name,
            "StartDate": start_date,
            "TimeZone": "Europe/Moscow",
            "UnifiedCampaign": {
                "CounterIds": {
                    "Items": [counter_id],
                },
                "BiddingStrategy": self.build_unified_bidding_strategy(
                    goal_id=goal_id,
                    cpa_micros=cpa_micros,
                    weekly_budget_micros=weekly_budget_micros,
                    placement_type=placement_type,
                ),
            },
        }

        if tracking_params is not None:
            campaign_item["UnifiedCampaign"]["TrackingParams"] = tracking_params

        response = self.call_v501(
            service="campaigns",
            method="add",
            params={
                "Campaigns": [
                    campaign_item
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
        tracking_params: Optional[str] = None,
        placement_type: Optional[str] = None,
    ) -> Dict[str, Any]:
        return self.add_unified_campaign(
            name=name,
            start_date=start_date,
            goal_id=goal_id,
            cpa_micros=cpa_micros,
            weekly_budget_micros=weekly_budget_micros,
            counter_id=counter_id,
            tracking_params=tracking_params,
            placement_type=placement_type,
        )

    def add_unified_campaign_production(
        self,
        name: str,
        start_date: str,
        goal_id: int,
        cpa_micros: int,
        weekly_budget_micros: int,
        counter_id: int = DEFAULT_METRICA_COUNTER_ID,
        tracking_params: Optional[str] = None,
        placement_type: Optional[str] = None,
    ) -> Dict[str, Any]:
        return self.add_unified_campaign(
            name=name,
            start_date=start_date,
            goal_id=goal_id,
            cpa_micros=cpa_micros,
            weekly_budget_micros=weekly_budget_micros,
            counter_id=counter_id,
            tracking_params=tracking_params,
            placement_type=placement_type,
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
        ad_image_hash: Optional[str] = None,
        sitelink_set_id: Optional[int] = None,
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

        if ad_image_hash is not None:
            ad_item["TextAd"]["AdImageHash"] = ad_image_hash

        if sitelink_set_id is not None:
            ad_item["TextAd"]["SitelinkSetId"] = sitelink_set_id

        print("DEBUG ADS PARAMS:", json.dumps({
            "Ads": [ad_item],
        }, ensure_ascii=False, indent=2))

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
        ad_image_hash: Optional[str] = None,
        sitelink_set_id: Optional[int] = None,
    ) -> Dict[str, Any]:
        return self.add_text_ad(
            ad_group_id=ad_group_id,
            title=title,
            text=text,
            href=href,
            display_url_path=display_url_path,
            ad_image_hash=ad_image_hash,
            sitelink_set_id=sitelink_set_id,
        )

    def add_text_ad_production(
        self,
        ad_group_id: int,
        title: str,
        text: str,
        href: str,
        display_url_path: Optional[str] = None,
        ad_image_hash: Optional[str] = None,
        sitelink_set_id: Optional[int] = None,
    ) -> Dict[str, Any]:
        return self.add_text_ad(
            ad_group_id=ad_group_id,
            title=title,
            text=text,
            href=href,
            display_url_path=display_url_path,
            ad_image_hash=ad_image_hash,
            sitelink_set_id=sitelink_set_id,
        )

    def update_text_ad_image(
        self,
        ad_id: int,
        ad_image_hash: Optional[str] = None,
        remove: bool = False,
    ) -> Dict[str, Any]:
        if not remove and ad_image_hash is None:
            raise YandexDirectClientError(
                "ad_image_hash must be provided unless remove=True"
            )

        response = self.call_v501(
            service="ads",
            method="update",
            params={
                "Ads": [
                    {
                        "Id": ad_id,
                        "TextAd": {
                            "AdImageHash": None if remove else ad_image_hash,
                        },
                    }
                ],
            },
        )
        return response.body

    def update_text_ad_sitelink_set_id(
        self,
        ad_id: int,
        sitelink_set_id: int,
    ) -> Dict[str, Any]:
        response = self.call_v501(
            service="ads",
            method="update",
            params={
                "Ads": [
                    {
                        "Id": ad_id,
                        "TextAd": {
                            "SitelinkSetId": sitelink_set_id,
                        },
                    }
                ],
            },
        )
        return response.body

    def set_text_ad_callout_ids(
        self,
        ad_id: int,
        ad_extension_ids: List[int],
    ) -> Dict[str, Any]:
        response = self.call_v501(
            service="ads",
            method="update",
            params={
                "Ads": [
                    {
                        "Id": ad_id,
                        "TextAd": {
                            "CalloutSetting": {
                                "AdExtensions": [
                                    {
                                        "AdExtensionId": ad_extension_id,
                                        "Operation": "SET",
                                    }
                                    for ad_extension_id in ad_extension_ids
                                ],
                            },
                        },
                    }
                ],
            },
        )
        return response.body

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
