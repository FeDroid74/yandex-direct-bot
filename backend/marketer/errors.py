import json
import re


def safe_error(value, limit=1600):
    text = str(value)
    text = re.sub(r"(?i)\b(Bearer|OAuth)\s+[\w.~-]+", r"\1 [REDACTED]", text)
    text = re.sub(r"\by0_[\w-]+", "[REDACTED]", text)
    text = re.sub(r"(?i)((?:api[_-]?key|access[_-]?token|refresh[_-]?token|password|authorization|botToken)[\"']?\s*[:=]\s*[\"']?)[^\s,\"'}]+",
                  r"\1[REDACTED]", text)
    return text[:limit]


class ReadError(RuntimeError):
    def __init__(self, provider, operation, *, status=None, payload=None, params=None):
        payload = payload if isinstance(payload, dict) else {}
        error = payload.get("error", payload)
        error = error if isinstance(error, dict) else {}
        details = {k: error[k] for k in ("error_code", "error_string", "error_detail", "request_id", "message") if k in error}
        if isinstance(payload.get("errors"), list):
            details["errors"] = [{k: item[k] for k in ("error_type", "message") if k in item}
                                 for item in payload["errors"][:3] if isinstance(item, dict)]
        self.diagnostic = {"provider": provider, "operation": operation, "http_status": status,
                           "details": json.loads(safe_error(json.dumps(details, ensure_ascii=False), 100000)),
                           "request": params or {}}
        super().__init__(safe_error(json.dumps(self.diagnostic, ensure_ascii=False)))


def failure(exc, **context):
    result = {**context, "error": safe_error(exc)}
    if isinstance(exc, ReadError):
        result["diagnostic"] = exc.diagnostic
    return result
