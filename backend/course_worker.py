"""Sequential private transcription of accessible, unprotected GetCourse lessons."""
import fcntl
import http.cookiejar
import ipaddress
import json
import os
import re
import shutil
import socket
import subprocess
import tempfile
import time
import urllib.request
from pathlib import Path
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup
import m3u8

from course_sync import HOST, RestrictedRedirect, init_queue
from course_audio import transcribe_audio
from marketer.knowledge import Knowledge
from marketer.store import Store, encode
from marketer.telegram import Telegram

# GetCourse manifests use different tenants of the same observed segment CDN.
MEDIA_DOMAINS = ("gceuproxy.com", "vhcdn.com", "kinescopecdn.net", "getcourse.ru", "a.trbcdn.net")


def media_url(url):
    p = urlsplit(url)
    if p.scheme != "https" or p.username or p.password or p.port not in (None, 443) or not any(p.hostname == d or (p.hostname or "").endswith("."+d) for d in MEDIA_DOMAINS):
        raise ValueError("Unapproved media host")
    if not all(ipaddress.ip_address(info[4][0]).is_global for info in socket.getaddrinfo(p.hostname, 443)):
        raise ValueError("Non-public media host")
    return url


class MediaRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return super().redirect_request(req, fp, code, msg, headers, media_url(newurl))


def media_text(url):
    op = urllib.request.build_opener(MediaRedirect())
    with op.open(media_url(url), timeout=45) as r:
        data = r.read(4*1024*1024+1)
        if len(data) > 4*1024*1024:
            raise ValueError("Oversized media manifest")
    return data.decode("utf-8-sig")


def playlist(url, depth=0):
    if depth > 3:
        raise ValueError("Playlist recursion limit")
    p = m3u8.loads(media_text(url), uri=url)
    if any(k and k.method != "NONE" for k in p.keys) or p.session_keys:
        raise ValueError("Encrypted/protected media is not imported")
    if p.is_variant:
        if not p.playlists:
            raise ValueError("Empty media variants")
        selected = min(p.playlists, key=lambda v: v.stream_info.bandwidth or float("inf"))
        return playlist(urljoin(url, selected.uri), depth+1)
    if not p.is_endlist or not p.segments:
        raise ValueError("Only finite recorded lessons can be imported")
    for s in p.segments:
        media_url(urljoin(url, s.uri))
        if s.init_section:
            media_url(urljoin(url, s.init_section.uri))
    return url, sum(s.duration for s in p.segments)


def run(store, private_dir):
    init_queue(store)
    generation = store.setting("course_discovery", {}).get("generation")
    with store.db() as db:
        db.execute("CREATE TABLE IF NOT EXISTS course_native (lesson_url TEXT PRIMARY KEY,status TEXT NOT NULL,data TEXT NOT NULL,updated REAL NOT NULL)")
        rows = db.execute("""SELECT l.* FROM course_lessons l LEFT JOIN course_native n ON n.lesson_url=l.url
            WHERE (? IS NULL OR json_extract(l.data,'$.generation')=?)
            AND (n.status IS NULL OR n.status IN ('processing','authentication_required')
                 OR json_extract(l.data,'$.native_signature') IS NOT json_extract(n.data,'$.native_signature')
                 OR (n.status='needs_attention' AND json_extract(n.data,'$.stage') IN ('lesson','audio_download')
                      AND n.updated < ?))
            ORDER BY CASE WHEN l.title LIKE '%Директ%' OR l.title LIKE '%Метрик%' OR l.title LIKE '%ключев%' THEN 0 ELSE 1 END,l.url""",
            (generation, generation, time.time()-6*86400)).fetchall()
    jar = http.cookiejar.LWPCookieJar(str(private_dir/"course-cookies.lwp"));jar.load(ignore_discard=True)
    op = urllib.request.build_opener(RestrictedRedirect(), urllib.request.HTTPCookieProcessor(jar))
    model = None
    store.set_setting("course_worker", {"status": "running", "queued_lessons": len(rows), "at": time.time(), "method": "local_speech_transcription"})
    def record(url, state, data):
        data["native_signature"] = signature
        with store.db() as db:
            db.execute("INSERT INTO course_native VALUES(?,?,?,?) ON CONFLICT(lesson_url) DO UPDATE SET status=excluded.status,data=excluded.data,updated=excluded.updated", (url, state, encode(data), time.time()))
    for row in rows:
        url = row["url"]
        signature = json.loads(row["data"]).get("native_signature")
        if urlsplit(url).hostname != HOST:
            continue
        record(url, "processing", {"title": row["title"]})
        stage = "lesson"
        try:
            with op.open(url, timeout=45) as r:
                page = r.read(4*1024*1024+1)
            if len(page) > 4*1024*1024:
                raise ValueError("Oversized lesson")
            soup = BeautifulSoup(page, "html.parser")
            if not soup.select(".gc-user-logined"):
                raise PermissionError("Course session expired")
            players = list(dict.fromkeys(e["data-iframe-src"] for e in soup.select(".lite-page [data-iframe-src]")))
            if not players:
                record(url, "no_native_video", {"title": row["title"], "youtube_or_text_may_remain": True})
                continue
            imported = []
            for index, player in enumerate(players, 1):
                stage = "player"
                raw = media_text(player)
                match = re.search(r"window\.configs\s*=\s*", raw)
                if not match:
                    raise ValueError("Unsupported player configuration")
                config = json.JSONDecoder().raw_decode(raw[match.end():])[0]
                if config.get("isVideoProtect") is not False or not config.get("isVideoReady"):
                    raise ValueError("Protected or unavailable video")
                file_id = str(config["gcFileId"])
                checkpoint = store.setting("course_transcript:"+file_id)
                if checkpoint:
                    sections = checkpoint["sections"]
                else:
                    stage = "manifest"
                    media, duration = playlist(config["masterPlaylistUrl"])
                    if not 0 < duration <= 4*3600:
                        raise ValueError("Video duration outside 0..4 hours")
                    if shutil.disk_usage(private_dir).free < 2*1024**3:
                        raise ValueError("Not enough free disk space for transcription")
                    from faster_whisper import WhisperModel
                    work = private_dir/"course-work"
                    work.mkdir(exist_ok=True, mode=0o700)
                    with tempfile.TemporaryDirectory(dir=work) as tmp:
                        stage = "audio_download"
                        audio = Path(tmp)/"audio.wav"
                        ffmpeg = shutil.which("ffmpeg")
                        if not ffmpeg:
                            raise RuntimeError("System ffmpeg is required")
                        command = [ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin",
                                   "-protocol_whitelist", "https,tls,tcp", "-rw_timeout", "30000000", "-f", "hls", "-allowed_extensions", "ALL", "-i", media,
                                   "-vn", "-ac", "1", "-ar", "16000", "-t", str(duration+2), str(audio)]
                        result = subprocess.run(command, capture_output=True, timeout=3600)
                        if result.returncode or not audio.exists():
                            raise RuntimeError("Authorized media audio download failed")
                        stage = "transcription"
                        if model is None:
                            model = WhisperModel("base", device="cpu", compute_type="int8", cpu_threads=1, num_workers=1)
                        sections, audio_duration = transcribe_audio(model, audio, index)
                        store.set_setting("course_transcript:"+file_id, {"sections": sections, "duration": audio_duration, "method": "faster-whisper base int8; automatic transcript, visual demonstrations not extracted"})
                stage = "knowledge_import"
                metadata = {"course": "Маркетинговые связки / Сергей Нагорный", "lesson": row["title"][:170]+f" [видео {index}, автотекст]", "url": url, "rights_confirmed": True}
                imported.append(Knowledge(store).ingest(metadata, sections)["source_id"])
            record(url, "imported", {"title": row["title"], "sources": imported, "videos": len(players), "method": "speech_transcript_not_visual_review"})
        except PermissionError:
            record(url, "authentication_required", {"title": row["title"]})
            store.set_setting("course_worker", {"status": "authentication_required", "at": time.time()})
            return
        except Exception as exc:
            record(url, "needs_attention", {"title": row["title"], "stage": stage, "error": type(exc).__name__})
        time.sleep(2)
    store.set_setting("course_worker", {"status": "finished_available_native_videos", "at": time.time()})
    with store.db() as db:
        counts = dict(db.execute("SELECT status,count(*) FROM course_native GROUP BY status").fetchall())
    key = "course_completion:"+str(store.setting("course_discovery", {}).get("checked_at"))
    if rows and not store.setting(key):
        store.set_setting(key, {"delivery": "uncertain"})
        try:
            config = json.loads((private_dir/"marketer.json").read_text())
            oc = json.loads(Path(config["openclaw_config"]).read_text())
            message = (f"Обработка доступных видео курса завершена. Уроков с импортированными расшифровками: {counts.get('imported', 0)}. "
                       f"Требуют внимания: {counts.get('needs_attention', 0)}. Без найденного штатного видео: {counts.get('no_native_video', 0)}. "
                       "Закрытые стоп-уроки не обрабатывались. Это распознавание речи, не полный визуальный разбор. Материалы доступны для анализа рекламы; настройки кампаний не менялись.")
            sent = Telegram(oc["channels"]["telegram"]["botToken"], config["owner_id"]).notify(message)
            store.set_setting(key, {"delivery": "sent", "message_id": sent["message_id"]})
        except Exception:
            pass  # An uncertain notification is not sent a second time.


if __name__ == "__main__":
    private = Path.home()/".config/yandex-direct-bot"
    lock = (private/"course-worker.lock").open("a")
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    os.chmod(lock.name, 0o600)
    db = Path(__file__).resolve().parents[1]/"runtime/marketer.sqlite3"
    run(Store(db), private)
