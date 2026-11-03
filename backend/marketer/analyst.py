import json
import re
import subprocess
import time

from marketer.errors import safe_error


class AnalystUnavailable(RuntimeError):
    pass


class NotDispatched(AnalystUnavailable):
    pass


def connection_failed_before_dispatch(error):
    # OpenClaw 2026.3.13 only calls the RPC from onHelloOk; this is connect's catch.
    return bool(re.search(r"(?m)^gateway connect failed: Error: gateway (?:closed \([0-9]+\):|not connected(?:\s|$))", str(error)))


def json_response(output):
    decoder = json.JSONDecoder()
    for index, char in enumerate(output):
        if char != "{":
            continue
        try:
            value, _ = decoder.raw_decode(output[index:])
        except ValueError:
            continue
        if isinstance(value, dict) and any(k in value for k in ("runId", "messages", "payloads", "result")):
            return value
    raise AnalystUnavailable("OpenClaw returned no structured response")


def answer_from(envelope):
    result = envelope.get("result", envelope)
    if result.get("meta", {}).get("stopReason") == "error":
        raise AnalystUnavailable("OpenClaw model returned an error; no analysis accepted")
    payloads = result.get("payloads")
    if isinstance(payloads, list):
        return "\n".join(p.get("text", "") for p in payloads)
    for message in reversed(result.get("messages", [])):
        if message.get("role") != "assistant":
            continue
        if message.get("stopReason") in ("error", "aborted", "length"):
            raise AnalystUnavailable("OpenClaw analysis did not finish normally")
        content = message.get("content", [])
        if isinstance(content, str):
            return content
        return "\n".join(part.get("text", "") for part in content if part.get("type") == "text")
    return None


class Analyst:
    """Persist before dispatch; recovery only reads the same isolated session."""

    def __init__(self, store, policy):
        self.store, self.policy = store, policy

    def rpc(self, method, params, timeout=40):
        try:
            completed = subprocess.run(
                [self.policy["openclaw_command"], "gateway", "call", method, "--params",
                 json.dumps(params, ensure_ascii=False), "--timeout", str((timeout - 5) * 1000), "--json"],
                capture_output=True, text=True, timeout=timeout)
        except (subprocess.TimeoutExpired, OSError) as exc:
            raise AnalystUnavailable(safe_error(f"OpenClaw {method}: {type(exc).__name__}")) from exc
        if completed.returncode:
            raw_error = getattr(completed, "stderr", "")
            error = safe_error(raw_error)
            if connection_failed_before_dispatch(raw_error) and not any(key in completed.stdout for key in ('"runId"', '"payloads"')):
                raise NotDispatched("OpenClaw agent connection failed before RPC dispatch: " + error[-1000:])
            if "OAuth token refresh failed" in raw_error:
                self.store.set_setting("analyst_auth", {"status": "reauthentication_required", "checked_at": time.time()})
                raise AnalystUnavailable("OpenClaw требует повторного входа в ChatGPT; OAuth не обновляется.")
            raise AnalystUnavailable(f"OpenClaw {method}: " + error[-1000:])
        return json_response(completed.stdout)

    def read_rpc(self, method, params, timeout=40):
        for attempt in range(3):
            try:
                return self.rpc(method, params, timeout)
            except AnalystUnavailable:
                if attempt == 2:
                    raise
                time.sleep(2 ** attempt)

    def call(self, run_id, step_id, context, prompt, validate):
        message = prompt + "\nДАННЫЕ:\n" + json.dumps(context, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        if len(message.encode()) > self.policy.get("model_request_max_bytes", 100000):
            raise ValueError("Model request exceeds bounded input budget, including instructions")
        step = self.store.analysis_step(run_id, step_id, message, self.policy["analyst_agent"])
        if step["status"] == "complete":
            return validate(step["output"])
        if step["status"] == "invalid":
            raise AnalystUnavailable("Stored analyst answer is invalid; inspect step " + step_id)
        answer = None
        try:
            if step["status"] in ("submitted", "uncertain") and connection_failed_before_dispatch(step.get("error")):
                history = self.read_rpc("chat.history", {"sessionKey": step["session_key"], "limit": 10})
                if history.get("messages") == []:
                    self.store.step_update(run_id, step_id, "not_dispatched", error=step["error"])
                    step["status"] = "not_dispatched"
            if step["status"] in ("planned", "not_dispatched") and self.store.claim_analysis_step(run_id, step_id):
                try:
                    for attempt in range(3):
                        try:
                            accepted = self.rpc("agent", {"agentId": self.policy["analyst_agent"], "message": message,
                                "sessionKey": step["session_key"], "idempotencyKey": step["request_id"],
                                "thinking": "medium", "timeout": 600, "deliver": False})
                            break
                        except NotDispatched as exc:
                            if attempt == 2:
                                self.store.step_update(run_id, step_id, "not_dispatched", error=safe_error(exc))
                                raise
                            time.sleep(2 ** attempt)
                    answer = answer_from(accepted)
                    if accepted.get("runId", step["request_id"]) != step["request_id"]:
                        raise AnalystUnavailable("Unexpected OpenClaw run identity")
                except NotDispatched:
                    raise
                except AnalystUnavailable as exc:
                    if "повторного входа" in str(exc):
                        raise
                    # A lost acknowledgement is not permission to submit another model run.
                    self.store.step_update(run_id, step_id, "submitted", error=safe_error(exc))
            deadline = time.monotonic() + self.policy.get("analyst_wait_seconds", 660)
            while not answer:
                history = self.read_rpc("chat.history", {"sessionKey": step["session_key"], "limit": 10})
                answer = answer_from(history)
                if answer:
                    break
                if time.monotonic() >= deadline:
                    raise AnalystUnavailable("Analyst result is not confirmed; resume this run, do not resubmit the request")
                state = self.read_rpc("agent.wait", {"runId": step["request_id"], "timeoutMs": 60000}, timeout=75)
                if state.get("status") == "error":
                    raise AnalystUnavailable("OpenClaw analyst failed: " + safe_error(state.get("error", "unknown error")))
                if state.get("status") not in ("ok", "timeout"):
                    raise AnalystUnavailable("Unexpected analyst wait state")
                if state.get("status") == "ok":
                    history = self.read_rpc("chat.history", {"sessionKey": step["session_key"], "limit": 10})
                    answer = answer_from(history)
                    if not answer:
                        raise AnalystUnavailable("Completed analyst run has no retrievable final answer")
            try:
                parsed = json.loads(answer)
                result = validate(parsed)
            except (ValueError, TypeError, KeyError) as exc:
                self.store.step_update(run_id, step_id, "invalid", error=safe_error(exc))
                raise
            self.store.step_update(run_id, step_id, "complete", output=result)
            self.store.set_setting("analyst_auth", {"status": "ready", "checked_at": time.time()})
            return result
        except Exception as exc:
            current = self.store.get_analysis_step(run_id, step_id)
            if current["status"] not in ("invalid", "not_dispatched"):
                self.store.step_update(run_id, step_id, "uncertain", error=safe_error(exc))
            auth = self.store.setting("analyst_auth", {})
            if auth.get("status") != "reauthentication_required":
                self.store.set_setting("analyst_auth", {"status": "unavailable", "checked_at": time.time(), "error": safe_error(exc)})
            raise
