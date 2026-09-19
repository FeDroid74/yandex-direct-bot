import contextlib
import hashlib
import json
import sqlite3
import time
import uuid
from pathlib import Path


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value):
    return hashlib.sha256(encode(value).encode()).hexdigest()


class Conflict(ValueError):
    pass


class Store:
    def __init__(self, path):
        self.path = str(path)
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with self.db() as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS proposals (
                    id TEXT PRIMARY KEY, revision INTEGER NOT NULL, state TEXT NOT NULL,
                    fingerprint TEXT NOT NULL, body TEXT NOT NULL, created REAL NOT NULL,
                    updated REAL NOT NULL, expires REAL NOT NULL, defer_until REAL,
                    delivery TEXT NOT NULL DEFAULT 'pending', message_id INTEGER,
                    result TEXT, applied_at REAL, evaluated_at REAL
                );
                CREATE INDEX IF NOT EXISTS proposal_fingerprint ON proposals(fingerprint);
                CREATE TABLE IF NOT EXISTS events (
                    seq INTEGER PRIMARY KEY, proposal_id TEXT, revision INTEGER,
                    event TEXT NOT NULL, actor TEXT NOT NULL, at REAL NOT NULL, data TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS runs (
                    id TEXT PRIMARY KEY, created REAL NOT NULL, status TEXT NOT NULL,
                    data TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            """)

    @contextlib.contextmanager
    def db(self):
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def unpack(row):
        if row is None:
            raise Conflict("Карточка не найдена.")
        out = dict(row)
        out["body"] = json.loads(out["body"])
        out["result"] = json.loads(out["result"]) if out.get("result") else None
        return out

    @staticmethod
    def event(db, row, event, actor, data=None):
        db.execute("INSERT INTO events(proposal_id,revision,event,actor,at,data) VALUES(?,?,?,?,?,?)",
                   (row["id"], row["revision"], event, actor, time.time(), encode(data or {})))

    def create(self, body):
        now = time.time()
        fingerprint = digest({"campaign_id": body["campaign_id"], "action": body["action"]})
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            old = db.execute("SELECT * FROM proposals WHERE fingerprint=? AND updated>? ORDER BY created DESC LIMIT 1",
                             (fingerprint, now - 90 * 86400)).fetchone()
            if old is not None and old["state"] not in ("expired", "stale", "failed"):
                return self.unpack(old), False
            pid = uuid.uuid4().hex[:12]
            db.execute("INSERT INTO proposals(id,revision,state,fingerprint,body,created,updated,expires) VALUES(?,1,'pending',?,?,?,?,?)",
                       (pid, fingerprint, encode(body), now, now, now + 7 * 86400))
            row = db.execute("SELECT * FROM proposals WHERE id=?", (pid,)).fetchone()
            self.event(db, row, "created", "analyst", body)
            return self.unpack(row), True

    def get(self, pid):
        with self.db() as db:
            return self.unpack(db.execute("SELECT * FROM proposals WHERE id=?", (pid,)).fetchone())

    def list(self, limit=100):
        with self.db() as db:
            return [self.unpack(r) for r in db.execute("SELECT * FROM proposals ORDER BY created DESC LIMIT ?", (limit,))]

    def revise(self, pid, revision, body):
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            old = self.unpack(db.execute("SELECT * FROM proposals WHERE id=?", (pid,)).fetchone())
            if old["revision"] != revision or old["state"] not in ("pending", "deferred"):
                raise Conflict("Карточка уже изменена или обработана. Откройте текущую версию.")
            if old["body"]["campaign_id"] != body["campaign_id"]:
                raise Conflict("Нельзя заменить кампанию в существующей карточке.")
            db.execute("UPDATE proposals SET revision=revision+1,state='pending',body=?,fingerprint=?,updated=?,expires=?,delivery='pending',defer_until=NULL WHERE id=?",
                       (encode(body), digest({"campaign_id": body["campaign_id"], "action": body["action"]}), time.time(), time.time()+7*86400, pid))
            row = db.execute("SELECT * FROM proposals WHERE id=?", (pid,)).fetchone()
            self.event(db, row, "revised_requires_new_approval", "analyst", body)
            return self.unpack(row)

    def decide(self, pid, revision, decision, actor):
        if decision not in ("approve", "reject", "defer", "done"):
            raise Conflict("Неизвестное решение.")
        now = time.time()
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = self.unpack(db.execute("SELECT * FROM proposals WHERE id=?", (pid,)).fetchone())
            if row["revision"] != revision:
                raise Conflict("Это старая версия карточки. Нужно подтвердить новую версию.")
            if decision == "done" and row["state"] == "accepted_manual":
                state = "completed_manual"
            elif row["state"] not in ("pending", "deferred"):
                return row, False
            elif row["expires"] < now:
                state = "expired"
            elif decision == "approve":
                state = "accepted_manual" if row["body"]["action"]["kind"] == "advisory" else "applying"
            elif decision == "done":
                raise Conflict("Сначала одобрите план ручной работы.")
            else:
                state = {"reject": "rejected", "defer": "deferred"}[decision]
            db.execute("UPDATE proposals SET state=?,updated=?,defer_until=? WHERE id=?",
                       (state, now, now+7*86400 if state == "deferred" else None, pid))
            self.event(db, row, decision, actor, {"state": state})
            row["state"] = state
            return row, True

    def finish(self, pid, state, result):
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = self.unpack(db.execute("SELECT * FROM proposals WHERE id=?", (pid,)).fetchone())
            db.execute("UPDATE proposals SET state=?,result=?,updated=?,applied_at=? WHERE id=?",
                       (state, encode(result), time.time(), time.time() if state == "applied" else None, pid))
            self.event(db, row, state, "executor", result)
        return self.get(pid)

    def delivery(self, pid, state, message_id=None):
        with self.db() as db:
            db.execute("UPDATE proposals SET delivery=?,message_id=COALESCE(?,message_id) WHERE id=?", (state, message_id, pid))

    def recover(self):
        with self.db() as db:
            # A crash after an API write is not evidence of failure. Never replay it.
            db.execute("UPDATE proposals SET state='uncertain' WHERE state='applying'")
            db.execute("UPDATE proposals SET delivery='uncertain' WHERE delivery='sending'")
            db.execute("UPDATE runs SET status='interrupted' WHERE status='running'")

    def save_run(self, run_id, status, data):
        with self.db() as db:
            db.execute("INSERT INTO runs VALUES(?,?,?,?) ON CONFLICT(id) DO UPDATE SET status=excluded.status,data=excluded.data",
                       (run_id, time.time(), status, encode(data)))

    def runs(self, limit=4):
        with self.db() as db:
            return [{**dict(r), "data": json.loads(r["data"])} for r in db.execute("SELECT * FROM runs ORDER BY created DESC LIMIT ?", (limit,))]

    def setting(self, key, default=None):
        with self.db() as db:
            row = db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
            return json.loads(row[0]) if row else default

    def set_setting(self, key, value):
        with self.db() as db:
            db.execute("INSERT INTO settings VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, encode(value)))

    def claim_schedule(self, slot):
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            return db.execute("INSERT OR IGNORE INTO settings VALUES(?,?)",
                              ("scheduled_slot:" + slot, encode({"started": time.time()}))).rowcount == 1

    def evaluate(self, pid, data):
        with self.db() as db:
            row = self.get(pid)
            self.event(db, row, "evaluation", "analyst", data)
            db.execute("UPDATE proposals SET evaluated_at=? WHERE id=?", (time.time(), pid))
