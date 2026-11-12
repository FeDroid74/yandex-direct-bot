"""Read accessible course pages and enqueue real videos; never complete assignments."""
import argparse
import hashlib
import http.cookiejar
import json
import os
import re
import time
import urllib.request
from pathlib import Path
from urllib.parse import urljoin, urlsplit, parse_qs

from marketer.store import Store, encode
from marketer.knowledge import Knowledge

HOST = "goraacademy.getcourse.ru"
ROOT = "https://"+HOST+"/teach/control/stream/view/id/690755990"


class RestrictedRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if urlsplit(newurl).scheme != "https" or urlsplit(newurl).hostname != HOST:
            raise ValueError("Course redirect left the authorized host")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def video_id(value):
    p = urlsplit(value)
    if p.hostname in ("youtu.be", "www.youtu.be"):
        value = p.path.strip("/")
    elif p.hostname in ("youtube.com", "www.youtube.com", "www.youtube-nocookie.com"):
        value = parse_qs(p.query).get("v", [""])[0] if p.path == "/watch" else p.path.removeprefix("/embed/")
    else:
        return None
    return value if re.fullmatch(r"[A-Za-z0-9_-]{11}", value) else None


def init_queue(store):
    with store.db() as db:
        db.executescript("""
            CREATE TABLE IF NOT EXISTS course_lessons (
                url TEXT PRIMARY KEY, title TEXT NOT NULL, data TEXT NOT NULL, checked REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS course_videos (
                video_id TEXT NOT NULL, lesson_url TEXT NOT NULL, title TEXT NOT NULL,
                status TEXT NOT NULL, source_id TEXT, error TEXT, updated REAL NOT NULL,
                PRIMARY KEY(video_id,lesson_url)
            );
        """)


def discover(store, cookie_file, bootstrap=None):
    from bs4 import BeautifulSoup
    jar = http.cookiejar.LWPCookieJar(str(cookie_file))
    if cookie_file.exists():
        jar.load(ignore_discard=True)
    opener = urllib.request.build_opener(RestrictedRedirect(), urllib.request.HTTPCookieProcessor(jar))
    def get(url):
        p = urlsplit(url)
        if p.scheme != "https" or p.hostname != HOST or p.username or p.password:
            raise ValueError("Only the authorized course host is allowed")
        with opener.open(url, timeout=45) as response:
            raw = response.read(4*1024*1024+1)
            if len(raw) > 4*1024*1024:
                raise ValueError("Course page too large")
        return BeautifulSoup(raw, "html.parser")
    if bootstrap:
        get(bootstrap)
    cookie_file.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(cookie_file.parent, 0o700)
    jar.save(ignore_discard=True)
    os.chmod(cookie_file, 0o600)
    queue, visited, lessons, locked = [ROOT], set(), {}, set()
    while queue:
        url = queue.pop(0)
        if url in visited:
            continue
        if len(visited) >= 40:
            raise ValueError("Course graph exceeds authorized bounded traversal")
        visited.add(url)
        soup = get(url)
        if not soup.select(".gc-user-logined"):
            raise ValueError("Course session expired; sign in again")
        for a in soup.select("a[href]"):
            target = urljoin(url, a["href"])
            p = urlsplit(target)
            if p.hostname != HOST:
                continue
            if re.fullmatch(r"/(?:pl/)?teach/control/stream/view/id/\d+", p.path) and target not in visited:
                queue.append(target)
            if re.fullmatch(r"/(?:pl/)?teach/control/lesson/view/id/\d+", p.path):
                lessons[target] = a.get_text(" ", strip=True)
        for item in soup.select("li"):
            if "Недоступен (стоп-урок)" in item.get_text(" ", strip=True) and not item.select("a[href]"):
                locked.add(item.get_text(" ", strip=True)[:400])
        time.sleep(.5)
    if not lessons:
        raise ValueError("No accessible lesson links, course not imported")
    if len(lessons) > 300:
        raise ValueError("Course exceeds maximum lesson count")
    init_queue(store)
    generation = time.time()
    for url, fallback in lessons.items():
        soup = get(url)
        if not soup.select(".gc-user-logined"):
            raise ValueError("Course session expired during discovery")
        title_node = soup.select_one("h2") or soup.select_one("title")
        title = title_node.get_text(" ", strip=True)[:200] if title_node else fallback[:200]
        blocks = soup.select(".lite-page")
        vids, native = set(), 0
        for block in blocks:
            for element in block.select("a[href], iframe[src]"):
                target = urljoin(url, element.get("href") or element.get("src"))
                vid = video_id(target)
                if vid:
                    vids.add(vid)
                elif element.name == "iframe":
                    native += 1
        info = {"youtube_ids": sorted(vids), "native_players": native, "readable_lesson": bool(blocks),
                "status": "queued" if vids else "native_media_required" if native else "no_video_found"}
        players = [e["data-iframe-src"] for e in soup.select(".lite-page [data-iframe-src]")]
        info["native_signature"] = hashlib.sha256(encode(players).encode()).hexdigest()
        info["generation"] = generation
        with store.db() as db:
            db.execute("INSERT INTO course_lessons VALUES(?,?,?,?) ON CONFLICT(url) DO UPDATE SET title=excluded.title,data=excluded.data,checked=excluded.checked", (url, title, encode(info), time.time()))
            for vid in vids:
                db.execute("INSERT OR IGNORE INTO course_videos VALUES(?,?,?,'pending',NULL,NULL,?)", (vid, url, title, time.time()))
        time.sleep(.5)
    jar.save(ignore_discard=True)
    store.set_setting("course_discovery", {"root": ROOT, "generation": generation, "streams": len(visited), "accessible_lessons": len(lessons), "locked_lesson_labels": sorted(locked), "checked_at": time.time(),
                                          "limits": "Only linked accessible lessons. No homework submitted, no locked lessons bypassed. A queued video is not an imported transcript."})
    return status(store)


def status(store):
    init_queue(store)
    with store.db() as db:
        counts = dict(db.execute("SELECT status,count(*) FROM course_videos GROUP BY status").fetchall())
        lessons = [json.loads(r[0]) for r in db.execute("SELECT data FROM course_lessons")]
    return {"discovery": store.setting("course_discovery"), "videos": counts,
            "native_only_lessons": sum(v["status"] == "native_media_required" for v in lessons),
            "lessons_without_found_video": sum(v["status"] == "no_video_found" for v in lessons)}


def import_transcript(store, vid, sections):
    init_queue(store)
    with store.db() as db:
        rows = db.execute("SELECT * FROM course_videos WHERE video_id=?", (vid,)).fetchall()
    if not rows:
        raise ValueError("Video must first be observed in an accessible course lesson")
    ids = []
    for r in rows:
        result = Knowledge(store).ingest({"course": "Маркетинговые связки / Сергей Нагорный", "lesson": r["title"],
                                        "url": r["lesson_url"], "rights_confirmed": True}, sections)
        with store.db() as db:
            db.execute("UPDATE course_videos SET status='imported',source_id=?,error=NULL,updated=? WHERE video_id=? AND lesson_url=?", (result["source_id"], time.time(), vid, r["lesson_url"]))
        ids.append(result["source_id"])
    return ids


def process_captions(store, maximum=200):
    from youtube_transcript_api import YouTubeTranscriptApi
    from youtube_transcript_api._errors import RequestBlocked, IpBlocked
    api = YouTubeTranscriptApi()
    with store.db() as db:
        rows = db.execute("SELECT DISTINCT video_id FROM course_videos WHERE status='pending' LIMIT ?", (maximum,)).fetchall()
    for row in rows:
        vid = row[0]
        try:
            transcript = api.fetch(vid, languages=["ru", "en"])
            sections = [{"locator": f"{int(s.start)//60}:{int(s.start)%60:02d}", "text": s.text} for s in transcript.snippets]
            import_transcript(store, vid, sections)
        except (RequestBlocked, IpBlocked):
            store.set_setting("course_worker", {"status": "provider_blocked", "at": time.time(), "next_action": "Use an authorized browser transcript export; no proxy or repeated requests."})
            return status(store)
        except Exception as exc:
            with store.db() as db:
                db.execute("UPDATE course_videos SET status='needs_transcript',error=?,updated=? WHERE video_id=? AND status='pending'", (type(exc).__name__, time.time(), vid))
        time.sleep(3)
    store.set_setting("course_worker", {"status": "finished_available_captions", "at": time.time()})
    return status(store)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["discover", "captions", "status", "import"])
    parser.add_argument("--db", type=Path, default=Path(__file__).resolve().parents[1]/"runtime/marketer.sqlite3")
    parser.add_argument("--cookies", type=Path, default=Path.home()/".config/yandex-direct-bot/course-cookies.lwp")
    parser.add_argument("--video-id")
    parser.add_argument("--transcript", type=Path)
    a = parser.parse_args()
    store = Store(a.db)
    if a.action == "discover":
        try:
            result = discover(store, a.cookies, os.environ.get("COURSE_BOOTSTRAP_URL"))
        except Exception as exc:
            # Keep the last good catalog. Do not misreport a stale import as a successful refresh.
            cause = "authentication_required" if "session expired" in str(exc).lower() else "discovery_failed"
            prior = store.setting("course_worker", {})
            store.set_setting("course_worker", {"status": cause, "at": time.time(), "error_type": type(exc).__name__})
            if prior.get("status") != cause:
                from marketer.telegram import Telegram
                conf = json.loads((Path.home()/".config/yandex-direct-bot/marketer.json").read_text())
                oc = json.loads(Path(conf["openclaw_config"]).read_text())
                text = "Для обновления курса нужен повторный вход в GetCourse." if cause == "authentication_required" else "Не удалось обновить каталог GetCourse; следующая попытка по расписанию."
                Telegram(oc["channels"]["telegram"]["botToken"], conf["owner_id"]).notify(text+" Ранее импортированные материалы сохранены.")
            raise
    elif a.action == "captions": result = process_captions(store)
    elif a.action == "import":
        from knowledge_import import sections_from_text
        if not a.transcript or not a.video_id: parser.error("import requires transcript and video-id")
        result = import_transcript(store, a.video_id, sections_from_text(a.transcript.read_text(encoding="utf-8-sig")))
    else: result = status(store)
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__": main()
