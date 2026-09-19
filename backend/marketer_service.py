import argparse
import hmac
import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from marketer.actions import Actions
from marketer.data import DataSource
from marketer.runner import Runner
from marketer.store import Conflict, Store
from marketer.telegram import Telegram, card


ROOT = Path(__file__).resolve().parent


def build_app():
    policy_path = Path(os.environ.get("MARKETER_POLICY", ROOT / "marketer/policy.json"))
    policy = json.loads(policy_path.read_text(encoding="utf-8"))
    private = Path(os.environ.get("MARKETER_PRIVATE_CONFIG", Path.home()/".config/yandex-direct-bot/marketer.json"))
    config = json.loads(private.read_text(encoding="utf-8"))
    oc = json.loads(Path(config["openclaw_config"]).read_text(encoding="utf-8"))
    telegram = Telegram(oc["channels"]["telegram"]["botToken"], config["owner_id"])
    store = Store(os.environ.get("MARKETER_DB", ROOT.parent/"runtime/marketer.sqlite3"))
    source = DataSource(policy)
    actions = Actions(source, store, policy)
    runner = Runner(store, source, actions, telegram, policy)
    return store, actions, runner, telegram, config


def serve():
    store, actions, runner, telegram, config = build_app()
    store.recover()
    run_lock = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            pass

        def reply(self, status, payload):
            raw = json.dumps(payload, ensure_ascii=False).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def do_GET(self):
            if self.path == "/health":
                self.reply(200, {"ok": True, "service": "marketer", "approval_required": True})
            else:
                self.reply(404, {"error": "not_found"})

        def do_POST(self):
            decision = self.path == "/decision"
            expected = config["decision_key"] if decision else config["api_key"]
            token = self.headers.get("Authorization", "").removeprefix("Bearer ")
            if not hmac.compare_digest(token, expected):
                self.reply(403, {"error": "forbidden"})
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 200000:
                    raise ValueError("Invalid request size")
                params = json.loads(self.rfile.read(length))
                if not isinstance(params, dict):
                    raise ValueError("Expected JSON object")
                if self.path == "/status":
                    runs = [{k: v for k, v in r.items() if k != "data"} for r in store.runs()]
                    result = {"runs": runs, "policy": runner.policy, "proposals": [{"id": r["id"], "revision": r["revision"], "state": r["state"], "title": r["body"]["title"]} for r in store.list(50)]}
                elif self.path == "/context":
                    result = runner.context(store.setting("latest_snapshot"))
                elif self.path == "/proposal":
                    result = store.get(params["id"])
                elif self.path in ("/propose", "/revise"):
                    snapshot = store.setting("latest_snapshot")
                    if not snapshot:
                        raise Conflict("Сначала нужен снимок аналитики.")
                    body = actions.prepare(params["proposal"], snapshot)
                    if self.path == "/revise":
                        result = store.revise(params["id"], params["revision"], body)
                    else:
                        result, _ = store.create(body)
                    telegram.publish(result, store)
                    result = store.get(result["id"])
                elif self.path == "/decision":
                    if str(params.get("sender_id")) != str(config["owner_id"]) or params.get("channel") != "telegram":
                        raise PermissionError("Only the Telegram owner may decide")
                    pid, revision, decision_name = params["id"], params["revision"], params["decision"]
                    row = store.get(pid)
                    if decision_name in ("details", "edit"):
                        result = {"text": card(row, details=True) if decision_name == "details" else
                                  f"Напишите: «Измени карточку {pid}: ...». Бот подготовит новую версию; она потребует отдельного подтверждения. Остальные карточки не изменятся."}
                    else:
                        row = actions.decide(pid, revision, decision_name, str(params["sender_id"]))
                        try:
                            telegram.refresh(row)
                        except RuntimeError:
                            pass
                        result = {"text": card(row), "state": row["state"]}
                elif self.path == "/run":
                    if not run_lock.acquire(blocking=False):
                        raise Conflict("Анализ уже выполняется.")
                    def work():
                        try:
                            runner.run(force=params.get("force") is True, collect_only=params.get("collect_only") is True)
                        except Exception:
                            pass  # Runner persists and notifies the failure.
                        finally:
                            run_lock.release()
                    threading.Thread(target=work, daemon=True).start()
                    result = {"status": "started"}
                else:
                    self.reply(404, {"error": "not_found"})
                    return
                self.reply(200, result)
            except PermissionError as exc:
                self.reply(403, {"error": str(exc)})
            except (ValueError, KeyError, TypeError) as exc:
                self.reply(409 if isinstance(exc, Conflict) else 400, {"error": str(exc)})
            except Exception as exc:
                self.reply(500, {"error": type(exc).__name__})

    server = ThreadingHTTPServer(("127.0.0.1", 8092), Handler)
    server.serve_forever()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--serve", action="store_true")
    args = parser.parse_args()
    if args.serve:
        serve()
