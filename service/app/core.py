import contextlib
import hashlib
import hmac
import json
import os
from pathlib import Path
import secrets
import sqlite3
import time

DATA = Path(os.environ.get("DATA_DIR", "/data"))
DB = DATA / "metadata" / "proctor.sqlite3"
RULES = {
    "reference": "ICO 2026 Rulebook v1, 11.3 and Appendix A",
    "container": "matroska", "video_encoder": "x264", "codec": "h264",
    "profile": "High", "rate_control": "crf", "crf": 24, "fps": 1,
    "min_width": 1280, "min_height": 720, "audio_codec": "aac",
    "sample_rate": 44100, "channels": 1, "round_seconds": 25200,
    "capture": "one full host display, uncropped and unobscured",
}
BUDGET = int(os.environ.get("STORAGE_BUDGET_BYTES", str(6 * 1024**4)))


def now():
    return time.time()


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def secret(name):
    filename = os.environ.get(name + "_FILE")
    if filename:
        return Path(filename).read_text().strip()
    value = os.environ.get(name, "")
    if not value:
        raise RuntimeError("Missing secret: " + name)
    return value


def password_hash(password):
    salt = secrets.token_hex(16)
    key = hashlib.scrypt(password.encode(), salt=salt.encode(), n=16384, r=8, p=1)
    return salt + "$" + key.hex()


def password_matches(password, value):
    try:
        salt, expected = value.split("$", 1)
        actual = hashlib.scrypt(password.encode(), salt=salt.encode(), n=16384, r=8, p=1)
        return hmac.compare_digest(actual.hex(), expected)
    except (ValueError, TypeError):
        return False


@contextlib.contextmanager
def db(immediate=False):
    conn = sqlite3.connect(DB, timeout=20)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=20000")
    try:
        if immediate:
            conn.execute("BEGIN IMMEDIATE")
        yield conn
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()


def initialize():
    for directory in ["metadata", "uploads", "originals", "dvr", "exports", "metadata-backups"]:
        (DATA / directory).mkdir(parents=True, exist_ok=True)
    with db() as c:
        c.execute("PRAGMA journal_mode=WAL")
        c.executescript("""
        CREATE TABLE IF NOT EXISTS users (
          username TEXT PRIMARY KEY, password_hash TEXT NOT NULL,
          role TEXT NOT NULL CHECK(role IN ('admin','proctor','student')),
          contestant_id TEXT, disabled INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE IF NOT EXISTS auth_sessions (
          token_hash TEXT PRIMARY KEY, username TEXT NOT NULL REFERENCES users(username),
          csrf TEXT NOT NULL, expires REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS login_attempts (identity TEXT, created REAL);
        CREATE INDEX IF NOT EXISTS attempts_identity ON login_attempts(identity, created);
        CREATE TABLE IF NOT EXISTS rounds (
          id TEXT PRIMARY KEY, name TEXT NOT NULL, expected_seconds INTEGER NOT NULL,
          start_at REAL, end_at REAL, enabled INTEGER NOT NULL DEFAULT 1);
        CREATE TABLE IF NOT EXISTS contestants (
          id TEXT PRIMARY KEY, name TEXT NOT NULL, group_name TEXT NOT NULL DEFAULT '',
          stream_key_hash TEXT, disabled INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE IF NOT EXISTS live_sessions (
          id TEXT PRIMARY KEY, contestant_id TEXT NOT NULL REFERENCES contestants(id),
          round_id TEXT NOT NULL REFERENCES rounds(id), client_id TEXT NOT NULL,
          started REAL NOT NULL, ended REAL, last_seen REAL, stats_json TEXT);
        CREATE INDEX IF NOT EXISTS live_active ON live_sessions(contestant_id, ended);
        CREATE TABLE IF NOT EXISTS uploads (
          id TEXT PRIMARY KEY, contestant_id TEXT NOT NULL REFERENCES contestants(id),
          round_id TEXT NOT NULL REFERENCES rounds(id), filename TEXT NOT NULL,
          size INTEGER NOT NULL, expected_sha256 TEXT NOT NULL,
          owner TEXT NOT NULL REFERENCES users(username), state TEXT NOT NULL,
          created REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS media (
          id TEXT PRIMARY KEY, contestant_id TEXT NOT NULL REFERENCES contestants(id),
          round_id TEXT NOT NULL REFERENCES rounds(id), kind TEXT NOT NULL,
          source_uid TEXT UNIQUE NOT NULL, path TEXT UNIQUE NOT NULL, filename TEXT NOT NULL,
          size INTEGER NOT NULL DEFAULT 0, state TEXT NOT NULL DEFAULT 'queued',
          sha256 TEXT, report_json TEXT, review TEXT NOT NULL DEFAULT 'pending',
          review_note TEXT NOT NULL DEFAULT '', reviewed_by TEXT, created REAL NOT NULL,
          finished REAL, capture_start REAL, capture_end REAL);
        CREATE TABLE IF NOT EXISTS jobs (
          id TEXT PRIMARY KEY, media_id TEXT UNIQUE NOT NULL REFERENCES media(id),
          state TEXT NOT NULL DEFAULT 'queued', attempts INTEGER NOT NULL DEFAULT 0,
          lease_until REAL, error TEXT, created REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS alerts (
          id INTEGER PRIMARY KEY, fingerprint TEXT UNIQUE NOT NULL, severity TEXT NOT NULL,
          contestant_id TEXT, title TEXT NOT NULL, detail TEXT NOT NULL DEFAULT '',
          created REAL NOT NULL, resolved REAL, acknowledged_by TEXT, acknowledged_at REAL);
        CREATE TABLE IF NOT EXISTS events (
          id INTEGER PRIMARY KEY, created REAL NOT NULL, actor TEXT NOT NULL,
          action TEXT NOT NULL, target TEXT NOT NULL, detail TEXT NOT NULL DEFAULT '');
        CREATE TABLE IF NOT EXISTS system_state (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        """)
        if not c.execute("SELECT 1 FROM users WHERE role='admin'").fetchone():
            c.execute("INSERT INTO users(username,password_hash,role) VALUES(?,?,'admin')",
                      (os.environ.get("ADMIN_USER", "admin"), password_hash(secret("ADMIN_PASSWORD"))))
        for rid, name in [("day-1", "比赛第一天"), ("day-2", "比赛第二天")]:
            c.execute("INSERT OR IGNORE INTO rounds(id,name,expected_seconds) VALUES(?,?,?)",
                      (rid, name, RULES["round_seconds"]))


def audit(c, actor, action, target="", detail=""):
    c.execute("INSERT INTO events(created,actor,action,target,detail) VALUES(?,?,?,?,?)",
              (now(), actor, action, target, detail))


def alert(c, fingerprint, title, detail="", contestant_id=None, severity="warning"):
    c.execute("""INSERT INTO alerts(fingerprint,severity,contestant_id,title,detail,created)
              VALUES(?,?,?,?,?,?) ON CONFLICT(fingerprint) DO UPDATE SET
              resolved=NULL, detail=excluded.detail, title=excluded.title,
              acknowledged_by=CASE WHEN alerts.resolved IS NOT NULL THEN NULL ELSE alerts.acknowledged_by END,
              acknowledged_at=CASE WHEN alerts.resolved IS NOT NULL THEN NULL ELSE alerts.acknowledged_at END""",
              (fingerprint, severity, contestant_id, title, detail, now()))


def resolve(c, fingerprint):
    c.execute("UPDATE alerts SET resolved=? WHERE fingerprint=? AND resolved IS NULL", (now(), fingerprint))


def set_state(c, key, value):
    c.execute("INSERT INTO system_state(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
              (key, json.dumps(value, ensure_ascii=False)))


def get_state(c, key, default=None):
    row = c.execute("SELECT value FROM system_state WHERE key=?", (key,)).fetchone()
    return json.loads(row[0]) if row else default


def safe_path(relative):
    path = (DATA / relative).resolve()
    if not path.is_relative_to(DATA.resolve()) or not path.is_file():
        raise ValueError("File outside data directory or missing")
    return path


def queue_media(c, *, contestant_id, round_id, kind, source_uid, path, filename,
                capture_start=None, capture_end=None):
    existing = c.execute("SELECT id FROM media WHERE source_uid=?", (source_uid,)).fetchone()
    if existing:
        return existing[0]
    mid = secrets.token_hex(16)
    size = safe_path(path).stat().st_size
    c.execute("""INSERT INTO media(id,contestant_id,round_id,kind,source_uid,path,filename,size,
              created,capture_start,capture_end) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
              (mid, contestant_id, round_id, kind, source_uid, path, filename, size,
               now(), capture_start, capture_end))
    c.execute("INSERT INTO jobs(id,media_id,created) VALUES(?,?,?)", (secrets.token_hex(16), mid, now()))
    return mid


def storage(c):
    v = os.statvfs(DATA)
    used = (v.f_blocks - v.f_bfree) * v.f_frsize
    available = v.f_bavail * v.f_frsize
    reserved = c.execute("SELECT COALESCE(SUM(size),0) FROM uploads WHERE state!='cancelled'").fetchone()[0]
    dvr = c.execute("SELECT COALESCE(SUM(size),0) FROM media WHERE kind='dvr'").fetchone()[0]
    return {"available_bytes": available, "used_fraction": used / (used + available),
            "budget_bytes": BUDGET, "committed_bytes": reserved + dvr,
            "budget_remaining_bytes": max(0, BUDGET - reserved - dvr)}
