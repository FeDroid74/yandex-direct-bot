from datetime import date, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
import json

from config import settings
from services.direct_client import YandexDirectClient, YandexDirectClientError
from services.direct_mock import validate_campaign


def rub_to_micros(value_rub: float) -> int:
    return int(float(value_rub) * 1_000_000)


def resolve_start_date(payload: dict) -> str:
    raw = payload.get("start_date")
    if isinstance(raw, str) and raw.strip():
        return raw.strip()
    return (date.today() + timedelta(days=1)).isoformat()


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

            if not settings.yandex_direct_use_sandbox:
                self._send_json(
                    {
                        "status": "error",
                        "message": "current real create endpoint is enabled only for sandbox stage",
                    },
                    400,
                )
                return

            client = YandexDirectClient()

            try:
                result = client.add_unified_campaign_sandbox(
                    name=data["campaign_name"],
                    start_date=resolve_start_date(data),
                    goal_id=int(data["metrica_goal_id"]),
                    average_cpa_micros=rub_to_micros(data["target_cpa_rub"]),
                )
            except YandexDirectClientError as e:
                self._send_json(
                    {
                        "status": "error",
                        "message": str(e),
                    },
                    502,
                )
                return

            add_results = result.get("result", {}).get("AddResults", [])
            if not add_results:
                self._send_json(
                    {
                        "status": "error",
                        "message": "empty AddResults from Yandex Direct",
                        "raw": result,
                    },
                    502,
                )
                return

            first = add_results[0]
            if "Errors" in first:
                self._send_json(
                    {
                        "status": "error",
                        "message": "Yandex Direct rejected campaign create",
                        "errors": first["Errors"],
                        "raw": result,
                    },
                    400,
                )
                return

            self._send_json(
                {
                    "status": "success",
                    "campaign_id": str(first["Id"]),
                    "target": "sandbox",
                    "start_date": resolve_start_date(data),
                    "data": data,
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