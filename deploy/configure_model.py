"""Register the supported replacement for the retired ChatGPT-login model.

Only the isolated analyst is changed initially. --direct also repairs the Direct
agent after the analyst smoke test passes. Unrelated agents remain unchanged.
"""
import json
import sys
from pathlib import Path

from install_marketer import private_json


home = Path.home()
path = home/".openclaw/openclaw.json"
config = json.loads(path.read_text())
model_id = "gpt-5.6-terra"
ref = "openai-codex/"+model_id
providers = config.setdefault("models", {}).setdefault("providers", {})
provider = providers.setdefault("openai-codex", {"baseUrl": "https://chatgpt.com/backend-api", "api": "openai-codex-responses", "auth": "oauth", "models": []})
if not any(m.get("id") == model_id for m in provider["models"]):
    provider["models"].append({"id": model_id, "name": "GPT-5.6 Terra", "api": "openai-codex-responses", "reasoning": True,
                                "input": ["text", "image"], "contextWindow": 128000, "maxTokens": 16000})
config["agents"]["defaults"].setdefault("models", {})[ref] = {}
for agent in config["agents"]["list"]:
    if agent["id"] == "direct-marketer" or ("--direct" in sys.argv and agent["id"] == "direct"):
        agent["model"] = ref
private_json(path, config)
print("Configured", ref, "for analyst" + (" and Direct agent" if "--direct" in sys.argv else " only"))
