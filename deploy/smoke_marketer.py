"""Read-only integration check; never calls a Direct mutation method."""
import json
import os
import shlex
import sys
from pathlib import Path


root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(root/"backend"))
for line in (Path.home()/".config/yandex-direct-bot/backend.env").read_text().splitlines():
    key, value = line.split("=", 1)
    os.environ[key] = json.loads(value)

from marketer_service import build_app

store, actions, runner, telegram, config = build_app()
mode = sys.argv[1] if len(sys.argv) > 1 else "collect"
if mode == "collect":
    # No Telegram notifications during initial API validation.
    runner.telegram = type("Quiet", (), {"notify": lambda _, text: print("NOTICE", text)})()
    print(json.dumps(runner.run(force=True, collect_only=True), ensure_ascii=False))
    store.set_setting("notification:data_errors", None)
elif mode == "status":
    for run in store.runs(1):
        print("run", run["id"], run["status"], "gate", run["data"].get("gate"))
        data = run["data"].get("snapshot") or store.setting("latest_snapshot", {})
        for c in data.get("campaigns", []):
            print(c["Id"], c["Name"], c["placement"], c["current"], "queries", len(c["queries"]), "ads", len(c["ads"]), "metrika_errors", c.get("metrika_errors"))
        print("errors", data.get("errors"))
elif mode == "analyze":
    snapshot = store.setting("latest_snapshot")
    analysis = runner.model(snapshot)
    valid, invalid = [], []
    for raw in analysis["proposals"]:
        try:
            valid.append(actions.prepare(raw, snapshot))
        except Exception as exc:
            invalid.append({"title": raw.get("title"), "error": str(exc)})
    store.set_setting("deployment_analysis", {"analysis": analysis, "valid": valid, "invalid": invalid})
    print(json.dumps({"summary": analysis.get("summary"), "valid": [{"title": p["title"], "action": p["action"]} for p in valid], "invalid": invalid}, ensure_ascii=False))
elif mode == "publish":
    result = store.setting("deployment_analysis")
    published = []
    for body in result["valid"]:
        row, fresh = store.create(body)
        if fresh:
            telegram.publish(row, store)
            published.append(row["id"])
        print(row["id"], row["state"], store.get(row["id"])["delivery"])
    store.set_setting("last_analyzed_snapshot", store.setting("latest_snapshot"))
    snapshot = store.setting("latest_snapshot")
    store.save_run(snapshot["run_id"], "complete", {"snapshot": snapshot, "summary": result["analysis"].get("summary"),
                                                   "published": published, "rejected": result["invalid"], "model_called": True})
