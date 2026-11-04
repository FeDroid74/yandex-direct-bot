"""Operator-side importer. Never stores credentials or publicly republishes a course."""
import argparse
import json
import re
from pathlib import Path

from marketer.knowledge import Knowledge
from marketer.store import Store


def sections_from_text(text):
    lines = text.splitlines()
    sections, current, locator = [], [], "text"
    for number, line in enumerate(lines, 1):
        timestamp = re.match(r"^\[([0-9:.,]+)\]\s*(.*)", line)
        srt = re.match(r"^(\d\d:\d\d:\d\d[,.]\d+)\s*-->", line)
        if timestamp or srt:
            if current and len(" ".join(current)) > 600:
                sections.append({"locator": locator, "text": "\n".join(current)})
                current = []
            if not current:
                locator = (timestamp or srt).group(1)
            if timestamp:
                current.append(timestamp.group(2))
        elif line.strip() and not line.strip().isdigit() and line.strip() != "WEBVTT":
            if not current and locator == "text":
                locator = f"line {number}"
            current.append(line)
    if current:
        sections.append({"locator": locator, "text": "\n".join(current)})
    return sections


def main():
    p = argparse.ArgumentParser()
    p.add_argument("file", type=Path)
    p.add_argument("--db", type=Path, default=Path(__file__).resolve().parents[1]/"runtime/marketer.sqlite3")
    p.add_argument("--course", required=True)
    p.add_argument("--lesson", required=True)
    p.add_argument("--url", default="")
    p.add_argument("--rights-confirmed", action="store_true")
    a = p.parse_args()
    if a.file.stat().st_size > 3*1024*1024:
        p.error("Text file too large; split by lesson")
    if a.file.suffix.casefold() not in (".txt", ".md", ".srt", ".vtt"):
        p.error("Use TXT/MD or actual SRT/VTT transcript, not a video filename")
    metadata = {"course": a.course, "lesson": a.lesson, "url": a.url, "rights_confirmed": a.rights_confirmed}
    print(json.dumps(Knowledge(Store(a.db)).ingest(metadata, sections_from_text(a.file.read_text(encoding="utf-8-sig"))), ensure_ascii=False))


if __name__ == "__main__":
    main()
