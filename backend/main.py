from http.server import BaseHTTPRequestHandler, HTTPServer
import json

from services.direct_mock import create_campaign, validate_campaign


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
            result = create_campaign(data)
            self._send_json(result, 200 if result.get("status") == "success" else 400)
            return

        self._send_json({"status": "error", "message": "not found"}, 404)


def main():
    server = HTTPServer(("127.0.0.1", 8091), Handler)
    print("Backend listening on http://127.0.0.1:8091")
    server.serve_forever()


if __name__ == "__main__":
    main()