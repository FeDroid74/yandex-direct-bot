import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Dict, Optional

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

    def call_v5(self, service: str, method: str, params: Dict[str, Any]) -> YandexDirectResponse:
        return self._call_with_base_url(
            base_url=self.base_url,
            service=service,
            method=method,
            params=params,
        )

    def call_v501(self, service: str, method: str, params: Dict[str, Any]) -> YandexDirectResponse:
        base_url_v501 = self.base_url.replace("/json/v5", "/json/v501")
        return self._call_with_base_url(
            base_url=base_url_v501,
            service=service,
            method=method,
            params=params,
        )

    def call(self, service: str, method: str, params: Dict[str, Any]) -> YandexDirectResponse:
        return self.call_v5(service, method, params)

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
        response = self.call_v5(
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
                ],
            },
        )
        return response.body

    def add_unified_campaign_sandbox(
        self,
        name: str,
        start_date: str,
        goal_id: int,
        average_cpa_micros: int,
    ) -> Dict[str, Any]:
            """
            Create UnifiedCampaign in sandbox with conversion-based strategy.

            Search:
            - AVERAGE_CPA
            Network:
            - NETWORK_DEFAULT
            """
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
                                "BiddingStrategy": {
                                    "Search": {
                                        "BiddingStrategyType": "AVERAGE_CPA",
                                        "AverageCpa": {
                                            "GoalId": goal_id,
                                            "AverageCpa": average_cpa_micros
                                        }
                                    },
                                    "Network": {
                                        "BiddingStrategyType": "NETWORK_DEFAULT"
                                    }
                                }
                            }
                        }
                    ]
                },
            )
            return response.body