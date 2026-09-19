"""The systemd timer only queues work; all model calls are gated in the service."""
import json
import urllib.request
from pathlib import Path


if __name__ == "__main__":
    config = json.loads((Path.home()/".config/yandex-direct-bot/marketer.json").read_text())
    req = urllib.request.Request("http://127.0.0.1:8092/run", data=b'{"scheduled":true}',
                                 headers={"Content-Type": "application/json", "Authorization": "Bearer "+config["api_key"]})
    with urllib.request.urlopen(req, timeout=30) as response:
        print(response.read().decode())
