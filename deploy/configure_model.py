"""Register the owner-selected ChatGPT-login model, preserving unrelated agents.

Only the isolated analyst is changed initially. --direct also repairs the Direct
agent after the analyst smoke test passes. Unrelated agents remain unchanged.
"""
import json
import sys
from pathlib import Path

from install_marketer import private_json


MODEL_ID = "gpt-6.1-sol"
MODEL_REF = "openai-codex/" + MODEL_ID


def configure(config, include_direct=False):
    provider = config.setdefault("models", {}).setdefault("providers", {}).setdefault("openai-codex", {
        "baseUrl": "https://chatgpt.com/backend-api", "api": "openai-codex-responses", "auth": "oauth", "models": []})
    if not any(m.get("id") == MODEL_ID for m in provider["models"]):
        # Keep the existing integration caps, not the model's advertised maximum.
        provider["models"].append({"id": MODEL_ID, "name": "GPT-6.1 Sol", "api": "openai-codex-responses", "reasoning": True,
                                    "input": ["text", "image"], "contextWindow": 128000, "maxTokens": 16000})
    config["agents"]["defaults"].setdefault("models", {}).setdefault(MODEL_REF, {})
    for agent in config["agents"]["list"]:
        if agent["id"] == "direct-marketer" or (include_direct and agent["id"] == "direct"):
            agent["model"] = MODEL_REF


if __name__ == "__main__":
    path = Path.home()/".openclaw/openclaw.json"
    config = json.loads(path.read_text())
    configure(config, include_direct="--direct" in sys.argv)
    private_json(path, config)
    print("Configured", MODEL_REF, "for analyst" + (" and Direct agent" if "--direct" in sys.argv else " only"))
