"""Version-pinned workaround for the observed 2026.3.13 compaction fallback."""
import argparse
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path


CHANGES = (
    ('const lastAssistant = messagesSnapshot.slice().toReversed().find((m) => m.role === "assistant");',
     'const lastAssistant = timedOutDuringCompaction ? undefined : messagesSnapshot.slice().toReversed().find((m) => m.role === "assistant");'),
    ('if (timedOut && !timedOutDuringCompaction && payloads.length === 0) return {',
     'if (timedOutDuringCompaction || (timedOut && payloads.length === 0)) return {'),
    ('text: "Request timed out before a response was generated.',
     'text: timedOutDuringCompaction ? "Не удалось обработать историю диалога вовремя. Нового ответа нет." : "Request timed out before a response was generated.'),
    ('Math.min(1e4, rawConnectDelayMs)) : 2e3;',
     'Math.min(1e4, rawConnectDelayMs)) : (process.env.YD_GATEWAY_LOCAL_RPC === "1" ? 1e4 : 2e3);'),
)

HANDSHAKE = (
    'const handshakeTimeoutMs = getHandshakeTimeoutMs();',
    'const handshakeTimeoutMs = !forwardedFor && ["127.0.0.1", "::1", "::ffff:127.0.0.1"].includes(remoteAddr) ? Math.max(15000, getHandshakeTimeoutMs()) : getHandshakeTimeoutMs();',
)


def transform(text):
    if all(text.count(new) == 1 for _, new in CHANGES):
        return text
    if not all(text.count(old) == 1 for old, _ in CHANGES):
        raise ValueError("Unsupported or partially patched OpenClaw bundle")
    for old, new in CHANGES:
        text = text.replace(old, new, 1)
    return text


def install(root, backup):
    if json.loads((root/"package.json").read_text())["version"] != "2026.3.13":
        raise ValueError("Review upstream behavior for this version before installing")
    targets = {}
    for p in (root/"dist").glob("*.js"):
        text = p.read_text()
        if CHANGES[0][0] in text or CHANGES[0][1] in text:
            targets[p] = transform(text)
    if not targets:
        raise ValueError("No supported reply handler found")
    gateway_bundles = 0
    for p in (root/"dist").glob("gateway-cli-*.js"):
        text = p.read_text()
        if HANDSHAKE[1] in text:
            gateway_bundles += 1
        elif text.count(HANDSHAKE[0]) == 1 and 'const DEFAULT_HANDSHAKE_TIMEOUT_MS = 3e3;' in text:
            targets[p] = text.replace(*HANDSHAKE)
            gateway_bundles += 1
    if not gateway_bundles:
        raise ValueError("No supported gateway handshake handler found")
    backup.mkdir(mode=0o700, parents=True, exist_ok=True)
    for p, text in targets.items():
        if text == p.read_text():
            continue
        if not (backup/p.name).exists():
            shutil.copy2(p, backup/p.name)
        with tempfile.NamedTemporaryFile(dir=p.parent, suffix=".mjs", mode="w", delete=False) as f:
            tmp = Path(f.name)
            f.write(text)
        try:
            subprocess.run(["node", "--check", str(tmp)], check=True)
            shutil.copymode(p, tmp)
            os.replace(tmp, p)
        finally:
            tmp.unlink(missing_ok=True)
    print("Verified reply guard in", len(targets), "bundles")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("root", type=Path)
    parser.add_argument("backup", type=Path)
    args = parser.parse_args()
    install(args.root, args.backup)
