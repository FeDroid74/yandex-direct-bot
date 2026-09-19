"""Run as appuser from the deployed repository. Never prints credentials."""
import json
import os
import secrets
import shlex
import shutil
import subprocess
from pathlib import Path


def private_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_suffix(".new")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2)+"\n", encoding="utf-8")
    temporary.chmod(0o600)
    temporary.replace(path)


def install():
    home = Path.home()
    repo = Path(__file__).resolve().parents[1]
    expected = home/".openclaw/workspace/yandex-direct-bot"
    if repo != expected:
        raise SystemExit("Unexpected deployment path")
    config_path = home/".openclaw/openclaw.json"
    config = json.loads(config_path.read_text())
    allowed = config["channels"]["telegram"]["allowFrom"]
    if "698291410" not in [str(v) for v in allowed]:
        raise SystemExit("Configured owner is not allowed by Telegram")
    private = home/".config/yandex-direct-bot"
    private.mkdir(parents=True, exist_ok=True, mode=0o700)
    private.chmod(0o700)
    credentials = {}
    launcher = (home/".openclaw/ydbot").read_text()
    for part in shlex.split(launcher.replace("\\\n", " ")):
        if part.startswith(("YANDEX_DIRECT_OAUTH_TOKEN=", "YANDEX_DIRECT_CLIENT_LOGIN=")):
            key, value = part.split("=", 1)
            credentials[key] = value
    if not credentials.get("YANDEX_DIRECT_OAUTH_TOKEN"):
        raise SystemExit("Existing backend launcher does not contain OAuth settings")
    envfile = private/"backend.env"
    envfile.write_text("\n".join(k+"="+json.dumps(v) for k,v in credentials.items())+"\n")
    envfile.chmod(0o600)
    private_path = private/"marketer.json"
    if not private_path.exists():
        private_json(private_path, {"owner_id": "698291410", "openclaw_config": str(config_path),
                                    "api_key": secrets.token_urlsafe(32), "decision_key": secrets.token_urlsafe(32)})
    plugins = config.setdefault("plugins", {})
    paths = plugins.setdefault("load", {}).setdefault("paths", [])
    old_path = str(repo/"integrations/openclaw-marketer")
    if old_path in paths:
        paths.remove(old_path)
    path = str(repo/"integrations/yandex-direct-marketer")
    if path not in paths:
        paths.append(path)
    plugins.setdefault("entries", {})["yandex-direct-marketer"] = {"enabled": True}
    if plugins.get("allow"):
        if "yandex-direct-marketer" not in plugins["allow"]:
            plugins["allow"].append("yandex-direct-marketer")
    telegram = config["channels"]["telegram"]
    caps = telegram.setdefault("capabilities", {})
    if isinstance(caps, dict):
        caps["inlineButtons"] = "allowlist"
    agents = config.setdefault("agents", {}).setdefault("list", [])
    if not any(a.get("id") == "direct-marketer" for a in agents):
        agents.append({"id": "direct-marketer", "name": "Read-only Direct Marketer",
                       "workspace": str(repo/"integrations/analyst"), "tools": {"deny": ["*"]}})
    private_json(config_path, config)
    # Same account, separate agent profile; only copy the existing authentication store.
    target = home/".openclaw/agents/direct-marketer/agent"
    target.mkdir(parents=True, exist_ok=True, mode=0o700)
    for agent in ("direct", "main"):
        auth = home/f".openclaw/agents/{agent}/agent/auth-profiles.json"
        if auth.exists() and not (target/"auth-profiles.json").exists():
            shutil.copy2(auth, target/"auth-profiles.json")
            (target/"auth-profiles.json").chmod(0o600)
    unitdir = home/".config/systemd/user"
    unitdir.mkdir(parents=True, exist_ok=True)
    for unit in (repo/"deploy/systemd").iterdir():
        shutil.copy2(unit, unitdir/unit.name)
    bridge = home/".npm-global/lib/node_modules/openclaw/extensions/yandex-direct-bridge"
    if bridge.is_dir():
        for name in ("index.ts", "openclaw.plugin.json"):
            shutil.copy2(repo/"integrations/yandex-direct-bridge"/name, bridge/name)
    subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
    subprocess.run(["systemctl", "--user", "enable", "--now", "yandex-direct-backend.service", "yandex-direct-marketer.service"], check=True)
    # Enable the timer only after a real collection and analyst smoke test succeeds.
    print("Installed services and isolated analyst. Weekly timer not yet enabled.")


if __name__ == "__main__":
    install()
