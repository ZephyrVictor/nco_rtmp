import csv
from datetime import datetime, timezone
from contextlib import asynccontextmanager
import hmac
from http.cookies import SimpleCookie
import io
import json
import math
import os
from pathlib import Path
import re
import secrets
import sqlite3
import zipfile
from urllib.parse import parse_qs, urlsplit

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from .core import (DATA, RULES, alert, audit, db, digest, get_state, initialize,
                   now, password_hash, password_matches, queue_media, resolve,
                   safe_path, secret, storage)


@asynccontextmanager
async def lifespan(app):
    initialize()
    yield


app = FastAPI(title="NCO Proctor", lifespan=lifespan, docs_url=None, redoc_url=None,
              openapi_url=None)
ID = r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$"
STARTED = now()


def check_worker(c):
    healthy = now() - get_state(c, "worker_at", STARTED) < 45
    if healthy:
        resolve(c, "service:worker")
    else:
        alert(c, "service:worker", "校验和监控进程心跳丢失", severity="critical")
    return healthy


def session_from_cookie(cookie):
    parsed = SimpleCookie()
    try:
        parsed.load(cookie or "")
    except Exception:
        return None
    token = parsed.get("nco_session")
    if not token:
        return None
    with db() as c:
        row = c.execute("""SELECT u.username,u.role,u.contestant_id,s.csrf,s.expires
                        FROM auth_sessions s JOIN users u ON u.username=s.username
                        WHERE s.token_hash=? AND s.expires>? AND u.disabled=0""",
                        (digest(token.value), now())).fetchone()
        if row and row["role"] == "student":
            valid = c.execute("SELECT 1 FROM contestants WHERE id=? AND disabled=0",
                              (row["contestant_id"],)).fetchone()
            if not valid:
                return None
        return dict(row) if row else None


def current_user(request: Request):
    user = session_from_cookie(request.headers.get("cookie"))
    if not user:
        raise HTTPException(401, "请先登录")
    return user


def staff(user=Depends(current_user)):
    if user["role"] not in ("admin", "proctor"):
        raise HTTPException(403, "需要监考权限")
    return user


def admin(user=Depends(current_user)):
    if user["role"] != "admin":
        raise HTTPException(403, "需要管理员权限")
    return user


def owns(user, contestant_id):
    return user["role"] in ("admin", "proctor") or user["contestant_id"] == contestant_id


@app.middleware("http")
async def protect_mutations(request, call_next):
    if request.url.path.startswith("/api/") and request.method in ("POST", "PUT", "PATCH", "DELETE"):
        origin = request.headers.get("origin")
        if origin and urlsplit(origin).netloc != request.headers.get("host"):
            return JSONResponse({"detail": "跨站请求已拒绝"}, 403)
        if request.url.path != "/api/auth/login":
            user = session_from_cookie(request.headers.get("cookie"))
            if not user:
                return JSONResponse({"detail": "请先登录"}, 401)
            if not hmac.compare_digest(user["csrf"], request.headers.get("x-csrf-token", "")):
                return JSONResponse({"detail": "请求校验失败，请重新登录"}, 403)
    response = await call_next(request)
    if request.url.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store"
    return response


class Login(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=256)


@app.post("/api/auth/login")
def login(body: Login, request: Request, response: Response):
    identity = (request.headers.get("x-real-ip") or request.client.host) + ":" + body.username
    with db() as c:
        c.execute("DELETE FROM login_attempts WHERE created<?", (now() - 86400,))
        count = c.execute("SELECT COUNT(*) FROM login_attempts WHERE identity=? AND created>?",
                          (identity, now() - 900)).fetchone()[0]
        if count >= 10:
            raise HTTPException(429, "登录尝试过多，请稍后重试")
        user = c.execute("SELECT * FROM users WHERE username=? AND disabled=0", (body.username,)).fetchone()
        valid = user and password_matches(body.password, user["password_hash"])
        if valid and user["role"] == "student":
            valid = bool(c.execute("SELECT 1 FROM contestants WHERE id=? AND disabled=0",
                                   (user["contestant_id"],)).fetchone())
        if not valid:
            c.execute("INSERT INTO login_attempts VALUES(?,?)", (identity, now()))
    if not valid:
        raise HTTPException(401, "账号或密码不正确")
    token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(24)
    with db() as c:
        c.execute("DELETE FROM auth_sessions WHERE expires<?", (now(),))
        c.execute("INSERT INTO auth_sessions VALUES(?,?,?,?)", (digest(token), body.username, csrf, now() + 43200))
        audit(c, body.username, "login")
    response.set_cookie("nco_session", token, max_age=43200, httponly=True,
                        secure=os.environ.get("COOKIE_SECURE", "false").lower() == "true",
                        samesite="strict", path="/")
    return {"username": body.username, "role": user["role"], "csrf": csrf,
            "contestant_id": user["contestant_id"]}


@app.post("/api/auth/logout")
def logout(request: Request, response: Response, user=Depends(current_user)):
    cookie = SimpleCookie(request.headers.get("cookie", ""))
    with db() as c:
        c.execute("DELETE FROM auth_sessions WHERE token_hash=?", (digest(cookie["nco_session"].value),))
        audit(c, user["username"], "logout")
    response.delete_cookie("nco_session", path="/")
    return {"ok": True}


@app.get("/api/auth/me")
def me(user=Depends(current_user)):
    return user


@app.get("/api/health")
def health():
    with db() as c:
        c.execute("SELECT 1")
    return {"ok": True}


@app.get("/api/rules")
def rules(user=Depends(current_user)):
    return {"rules": RULES, "manual_checks": ["宿主机完整屏幕", "画布与显示器一致", "无裁切、遮挡或暂停", "覆盖整场及异常通报"]}


@app.get("/api/overview")
def overview(user=Depends(current_user)):
    with db() as c:
        worker_ok = check_worker(c)
        state = get_state(c, "monitor", {})
        where, params = ("", ()) if user["role"] != "student" else (" WHERE contestant_id=?", (user["contestant_id"],))
        media = c.execute("SELECT kind,state,COUNT(*) n FROM media" + where + " GROUP BY kind,state", params).fetchall()
        alerts = c.execute("SELECT COUNT(*) FROM alerts WHERE resolved IS NULL" +
                           (" AND contestant_id=?" if params else ""), params).fetchone()[0]
        contestants = c.execute("SELECT COUNT(*) FROM contestants WHERE disabled=0").fetchone()[0]
        online = c.execute("SELECT COUNT(*) FROM live_sessions WHERE ended IS NULL" +
                           (" AND contestant_id=?" if params else ""), params).fetchone()[0]
        jobs = c.execute("SELECT state,COUNT(*) n FROM jobs GROUP BY state").fetchall() if not params else []
        return {"contestants": contestants if not params else 1, "online": online,
                "alerts": alerts, "media_counts": [dict(r) for r in media],
                "jobs": [dict(r) for r in jobs], "storage": storage(c) if not params else None,
                "monitor": state, "worker_at": get_state(c, "worker_at", 0),
                "worker_ok": worker_ok,
                "backup": get_state(c, "backup", {"configured": False}),
                "ingress_budget_mbps": 400}


class Candidate(BaseModel):
    id: str = Field(pattern=ID)
    name: str = Field(min_length=1, max_length=100)
    group_name: str = Field(default="", max_length=100)


class CandidateEdit(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    group_name: str = Field(default="", max_length=100)
    disabled: bool = False


@app.get("/api/contestants")
def contestants(user=Depends(current_user)):
    with db() as c:
        rows = c.execute("""SELECT id,name,group_name,disabled,
                          stream_key_hash IS NOT NULL AS has_key FROM contestants""" +
                         (" WHERE id=?" if user["role"] == "student" else "") + " ORDER BY id",
                         (user["contestant_id"],) if user["role"] == "student" else ()).fetchall()
        state = get_state(c, "monitor", {})
        fresh = now() - state.get("checked_at", 0) < 20 and state.get("srs_ok", False)
        result = []
        for row in rows:
            item = dict(row)
            live = c.execute("SELECT * FROM live_sessions WHERE contestant_id=? AND ended IS NULL ORDER BY started DESC LIMIT 1",
                             (row["id"],)).fetchone()
            item["live"] = dict(live) if live else None
            if item["live"]:
                item["live"]["stats"] = json.loads(item["live"].pop("stats_json") or "{}")
            item["status"] = ("online" if live else "offline") if fresh else "unknown"
            item["originals"] = c.execute("SELECT COUNT(*) FROM media WHERE contestant_id=? AND kind='original'", (row["id"],)).fetchone()[0]
            item["active_alerts"] = c.execute("SELECT COUNT(*) FROM alerts WHERE contestant_id=? AND resolved IS NULL", (row["id"],)).fetchone()[0]
            result.append(item)
        return result


@app.post("/api/contestants")
def add_candidate(body: Candidate, user=Depends(admin)):
    try:
        with db() as c:
            c.execute("INSERT INTO contestants(id,name,group_name) VALUES(?,?,?)", (body.id, body.name, body.group_name))
            audit(c, user["username"], "contestant.create", body.id)
    except sqlite3.IntegrityError:
        raise HTTPException(409, "选手编号已存在")
    return {"ok": True}


@app.put("/api/contestants/{cid}")
def edit_candidate(cid: str, body: CandidateEdit, user=Depends(admin)):
    with db() as c:
        if not c.execute("SELECT 1 FROM contestants WHERE id=?", (cid,)).fetchone():
            raise HTTPException(404, "选手不存在")
        if body.disabled and c.execute("SELECT 1 FROM live_sessions WHERE contestant_id=? AND ended IS NULL", (cid,)).fetchone():
            raise HTTPException(409, "选手正在推流，须先停止推流再停用")
        c.execute("UPDATE contestants SET name=?,group_name=?,disabled=? WHERE id=?", (body.name, body.group_name, body.disabled, cid))
        audit(c, user["username"], "contestant.update", cid)
    return {"ok": True}


class ImportCSV(BaseModel):
    csv: str = Field(max_length=1_000_000)


@app.post("/api/contestants/import")
def import_candidates(body: ImportCSV, user=Depends(admin)):
    try:
        rows = list(csv.DictReader(io.StringIO(body.csv.lstrip("\ufeff"))))
        if not rows or len(rows) > 2000:
            raise ValueError("名单须包含 1–2000 行")
        parsed = [Candidate(id=(r.get("id") or "").strip(), name=(r.get("name") or "").strip(),
                            group_name=(r.get("group") or "").strip()) for r in rows]
        if len({r.id for r in parsed}) != len(parsed):
            raise ValueError("名单包含重复编号")
    except (ValueError, csv.Error) as e:
        raise HTTPException(422, str(e))
    with db() as c:
        for row in parsed:
            c.execute("""INSERT INTO contestants(id,name,group_name) VALUES(?,?,?)
                      ON CONFLICT(id) DO UPDATE SET name=excluded.name,group_name=excluded.group_name""",
                      (row.id, row.name, row.group_name))
        audit(c, user["username"], "contestant.import", detail=str(len(parsed)))
    return {"imported": len(parsed)}


@app.post("/api/contestants/{cid}/key")
def candidate_key(cid: str, round_id: str = "day-1", user=Depends(current_user)):
    if user["role"] == "proctor" or not owns(user, cid):
        raise HTTPException(403, "无权重置推流凭据")
    with db(immediate=True) as c:
        if not c.execute("SELECT 1 FROM contestants WHERE id=? AND disabled=0", (cid,)).fetchone():
            raise HTTPException(404, "选手不存在")
        if not c.execute("SELECT 1 FROM rounds WHERE id=? AND enabled=1", (round_id,)).fetchone():
            raise HTTPException(404, "场次不存在")
        if c.execute("SELECT 1 FROM live_sessions WHERE contestant_id=? AND ended IS NULL", (cid,)).fetchone():
            raise HTTPException(409, "选手正在推流，暂不能重置凭据")
        token = secrets.token_urlsafe(32)
        c.execute("UPDATE contestants SET stream_key_hash=? WHERE id=?", (digest(token), cid))
        audit(c, user["username"], "stream.key.rotate", cid)
    return {"server": os.environ.get("INGEST_URL", "rtmp://10.77.0.1:1935/live"),
            "stream_key": f"{cid}?token={token}&round={round_id}", "round_id": round_id}


class UserCreate(BaseModel):
    username: str = Field(pattern=ID)
    password: str = Field(min_length=14, max_length=256)
    role: str = Field(pattern="^(admin|proctor|student)$")
    contestant_id: str | None = None


@app.get("/api/users")
def users(user=Depends(admin)):
    with db() as c:
        return [dict(r) for r in c.execute("SELECT username,role,contestant_id,disabled FROM users ORDER BY username")]


@app.post("/api/users")
def add_user(body: UserCreate, user=Depends(admin)):
    with db() as c:
        if body.role == "student" and not c.execute("SELECT 1 FROM contestants WHERE id=?", (body.contestant_id,)).fetchone():
            raise HTTPException(422, "选手账号必须绑定有效选手")
        try:
            c.execute("INSERT INTO users(username,password_hash,role,contestant_id) VALUES(?,?,?,?)",
                      (body.username, password_hash(body.password), body.role,
                       body.contestant_id if body.role == "student" else None))
        except sqlite3.IntegrityError:
            raise HTTPException(409, "账号已存在")
        audit(c, user["username"], "user.create", body.username, body.role)
    return {"ok": True}


class PasswordChange(BaseModel):
    current_password: str = Field(max_length=256)
    new_password: str = Field(min_length=14, max_length=256)


@app.post("/api/auth/password")
def change_password(body: PasswordChange, user=Depends(current_user)):
    with db() as c:
        row = c.execute("SELECT password_hash FROM users WHERE username=?", (user["username"],)).fetchone()
        if not password_matches(body.current_password, row[0]):
            raise HTTPException(403, "当前密码不正确")
        c.execute("UPDATE users SET password_hash=? WHERE username=?", (password_hash(body.new_password), user["username"]))
        c.execute("DELETE FROM auth_sessions WHERE username=?", (user["username"],))
        audit(c, user["username"], "user.password.change", user["username"])
    return {"ok": True, "login_required": True}


class UserUpdate(BaseModel):
    disabled: bool


@app.put("/api/users/{username}")
def disable_user(username: str, body: UserUpdate, user=Depends(admin)):
    if username == user["username"]:
        raise HTTPException(409, "不能停用当前账号")
    with db(immediate=True) as c:
        target = c.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
        if not target:
            raise HTTPException(404, "账号不存在")
        if body.disabled and target["role"] == "admin" and c.execute("SELECT COUNT(*) FROM users WHERE role='admin' AND disabled=0").fetchone()[0] <= 1:
            raise HTTPException(409, "必须保留一个管理员")
        c.execute("UPDATE users SET disabled=? WHERE username=?", (body.disabled, username))
        c.execute("DELETE FROM auth_sessions WHERE username=?", (username,))
        audit(c, user["username"], "user.update", username, str(body.disabled))
    return {"ok": True}


@app.get("/api/rounds")
def rounds(user=Depends(current_user)):
    with db() as c:
        return [dict(r) for r in c.execute("SELECT * FROM rounds ORDER BY id")]


class RoundEdit(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    start_at: float | None = None
    enabled: bool = True


@app.put("/api/rounds/{rid}")
def edit_round(rid: str, body: RoundEdit, user=Depends(admin)):
    with db(immediate=True) as c:
        row = c.execute("SELECT * FROM rounds WHERE id=?", (rid,)).fetchone()
        if not row:
            raise HTTPException(404, "场次不存在")
        if c.execute("SELECT 1 FROM live_sessions WHERE round_id=? AND ended IS NULL", (rid,)).fetchone():
            raise HTTPException(409, "场次有正在推流的选手，不能更改")
        if body.start_at is not None and not math.isfinite(body.start_at):
            raise HTTPException(422, "时间不正确")
        c.execute("UPDATE rounds SET name=?,start_at=?,end_at=?,enabled=? WHERE id=?",
                  (body.name, body.start_at, body.start_at + RULES["round_seconds"] if body.start_at is not None else None, body.enabled, rid))
        audit(c, user["username"], "round.update", rid)
    return {"ok": True}


@app.get("/api/media")
def media_list(contestant: str = "", round_id: str = "", kind: str = "", offset: int = 0,
               limit: int = 100, user=Depends(current_user)):
    clauses, args = [], []
    if user["role"] == "student":
        clauses.append("m.contestant_id=?")
        args.append(user["contestant_id"])
    elif contestant:
        clauses.append("m.contestant_id=?")
        args.append(contestant)
    if round_id:
        clauses.append("m.round_id=?")
        args.append(round_id)
    if kind:
        clauses.append("m.kind=?")
        args.append(kind)
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    with db() as c:
        total = c.execute("SELECT COUNT(*) FROM media m" + where, args).fetchone()[0]
        rows = c.execute("""SELECT m.*,c.name AS contestant_name FROM media m JOIN contestants c
                         ON c.id=m.contestant_id""" + where + " ORDER BY m.created DESC LIMIT ? OFFSET ?",
                         args + [min(max(limit, 1), 200), max(offset, 0)]).fetchall()
    items = []
    for row in rows:
        item = dict(row)
        item.pop("path")
        item["report"] = json.loads(item.pop("report_json") or "null")
        items.append(item)
    return {"total": total, "items": items}


def media_for_user(mid, user):
    with db() as c:
        row = c.execute("SELECT * FROM media WHERE id=?", (mid,)).fetchone()
    if not row or not owns(user, row["contestant_id"]):
        raise HTTPException(404, "录像不存在")
    return dict(row)


@app.get("/api/media/{mid}/download")
def download(mid: str, user=Depends(current_user)):
    item = media_for_user(mid, user)
    if item["kind"] == "original" and item["state"] not in ("checked", "failed"):
        raise HTTPException(409, "原件仍在处理")
    try:
        path = safe_path(item["path"])
    except ValueError:
        raise HTTPException(404, "文件暂不可用")
    with db() as c:
        audit(c, user["username"], "media.download", mid)
    return FileResponse(path, filename=item["filename"], media_type="application/octet-stream")


@app.get("/api/media/{mid}/report")
def media_report(mid: str, user=Depends(current_user)):
    item = media_for_user(mid, user)
    return {"id": mid, "state": item["state"], "sha256": item["sha256"],
            "review": item["review"], "review_note": item["review_note"],
            "report": json.loads(item["report_json"] or "null")}


@app.post("/api/media/{mid}/retry")
def retry_media(mid: str, user=Depends(admin)):
    media_for_user(mid, user)
    with db(immediate=True) as c:
        row = c.execute("SELECT state FROM jobs WHERE media_id=?", (mid,)).fetchone()
        if row and row[0] == "running":
            raise HTTPException(409, "任务正在运行")
        c.execute("UPDATE jobs SET state='queued',attempts=0,error=NULL,lease_until=NULL WHERE media_id=?", (mid,))
        c.execute("UPDATE media SET state='queued',review='pending',review_note='',reviewed_by=NULL WHERE id=?", (mid,))
        audit(c, user["username"], "media.retry", mid)
    return {"ok": True}


class Review(BaseModel):
    review: str = Field(pattern="^(accepted|rejected|pending)$")
    note: str = Field(min_length=1, max_length=2000)


@app.post("/api/media/{mid}/review")
def review(mid: str, body: Review, user=Depends(staff)):
    item = media_for_user(mid, user)
    if item["kind"] != "original" or item["state"] != "checked":
        raise HTTPException(409, "仅能审核已校验的原件")
    report = json.loads(item["report_json"] or "{}")
    if body.review == "accepted" and not report.get("parameters_pass"):
        raise HTTPException(409, "自动校验存在问题，须先处理并重新校验，不能标记合规")
    with db() as c:
        c.execute("UPDATE media SET review=?,review_note=?,reviewed_by=? WHERE id=?",
                  (body.review, body.note, user["username"], mid))
        audit(c, user["username"], "media.review", mid, body.note)
    return {"ok": True}


@app.get("/api/uploads")
def uploads(user=Depends(current_user)):
    with db() as c:
        rows = c.execute("SELECT * FROM uploads" + (" WHERE contestant_id=?" if user["role"] == "student" else "") + " ORDER BY created DESC LIMIT 500",
                         (user["contestant_id"],) if user["role"] == "student" else ()).fetchall()
    result = []
    for row in rows:
        item = dict(row)
        path = DATA / "uploads" / item["id"]
        item["offset"] = min(path.stat().st_size, item["size"]) if path.exists() else 0
        result.append(item)
    return result


@app.get("/api/alerts")
def alerts(user=Depends(staff)):
    with db() as c:
        return [dict(r) for r in c.execute("SELECT * FROM alerts ORDER BY resolved IS NULL DESC, created DESC LIMIT 300")]


class Note(BaseModel):
    note: str = Field(min_length=1, max_length=2000)


@app.post("/api/alerts/{aid}/ack")
def acknowledge(aid: int, body: Note, user=Depends(staff)):
    with db() as c:
        if not c.execute("SELECT 1 FROM alerts WHERE id=?", (aid,)).fetchone():
            raise HTTPException(404, "告警不存在")
        c.execute("UPDATE alerts SET acknowledged_by=?,acknowledged_at=? WHERE id=?", (user["username"], now(), aid))
        audit(c, user["username"], "alert.ack", str(aid), body.note)
    return {"ok": True}


@app.get("/api/events")
def events(user=Depends(staff)):
    with db() as c:
        return [dict(r) for r in c.execute("SELECT * FROM events ORDER BY id DESC LIMIT 300")]


@app.get("/api/manifest.csv")
def manifest(user=Depends(staff)):
    output = io.StringIO()
    columns = ["id", "contestant_id", "round_id", "kind", "filename", "path", "size", "state", "sha256", "review", "review_note", "created_utc", "finished_utc", "capture_start_utc", "capture_end_utc", "duration_seconds", "video", "audio", "decode_ok", "parameters_pass", "errors", "warnings"]
    writer = csv.writer(output)
    writer.writerow(columns)
    with db() as c:
        for row in c.execute("SELECT * FROM media ORDER BY contestant_id,round_id,created"):
            item=dict(row)
            report=json.loads(row['report_json'] or '{}')
            for key in ['created','finished','capture_start','capture_end']:
                item[key+'_utc']=datetime.fromtimestamp(row[key],timezone.utc).isoformat() if row[key] else ''
            streams=report.get('probe',{}).get('streams',[])
            item.update({k:report.get(k,'') for k in ['duration_seconds','decode_ok','parameters_pass']})
            for kind in ['video','audio']:
                item[kind]=json.dumps([s for s in streams if s.get('codec_type')==kind],ensure_ascii=False)
            for kind in ['errors','warnings']:
                item[kind]='; '.join(report.get(kind,[]))
            values=[item.get(k,'') for k in columns]
            writer.writerow(["'"+v if isinstance(v,str) and v.lstrip().startswith(('=','+','-','@')) else v for v in values])
        audit(c, user["username"], "manifest.export")
    return Response("\ufeff" + output.getvalue(), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": 'attachment; filename="recordings-manifest.csv"'})


@app.get("/api/obs-profile.zip")
def obs_profile(width: int = 1920, height: int = 1080, user=Depends(current_user)):
    if not (1280 <= width <= 8192 and 720 <= height <= 8192):
        raise HTTPException(422, "请填写宿主机的实际屏幕分辨率，最低 1280×720")
    scale = max(1280 / width, 720 / height)
    out_w, out_h = round(width * scale / 2) * 2, round(height * scale / 2) * 2
    name = "ICO2026"
    ini = f"""[General]
Name=ICO2026

[Video]
BaseCX={width}
BaseCY={height}
OutputCX={out_w}
OutputCY={out_h}
FPSType=1
FPSInt=1

[Output]
Mode=Advanced

[AdvOut]
RecType=Standard
RecFormat=mkv
RecFormat2=mkv
RecEncoder=obs_x264
RecAEncoder=ffmpeg_aac
RecTracks=1
RecRescale=false
Encoder=obs_x264
AudioEncoder=ffmpeg_aac
TrackIndex=1
Track1Bitrate=96
Rescale=false

[Audio]
SampleRate=44100
ChannelSetup=Mono
"""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(name + "/basic.ini", ini)
        archive.writestr(name + "/recordEncoder.json", json.dumps({"rate_control": "CRF", "crf": 24,
                                                                  "profile": "high", "preset": "veryfast", "keyint_sec": 2}))
        archive.writestr(name + "/streamEncoder.json",json.dumps({"rate_control":"CBR","bitrate":900,
                          "profile":"high","preset":"veryfast","tune":"zerolatency","keyint_sec":2}))
        archive.writestr("CHECKLIST.txt", "OBS profile export: import the ICO2026 directory as a profile.\n"
                          "Select a writable local recording path. Restart OBS after audio changes.\n"
                          "Create exactly one host Display Capture; fit the complete display without crops, filters or overlays.\n"
                          "The profile does not select your physical monitor or create a portable capture source.\n"
                          "Record a test and verify the resulting MKV before the contest.\n")
    return Response(buffer.getvalue(), media_type="application/zip", headers={"Content-Disposition": 'attachment; filename="ICO2026-OBS-profile.zip"'})


@app.get("/internal/authorize")
def authorize(request: Request):
    user = current_user(request)
    uri = urlsplit(request.headers.get("x-original-uri", "")).path
    method = request.headers.get("x-original-method", "GET")
    if uri.startswith("/live/"):
        if user["role"] not in ("admin", "proctor"):
            raise HTTPException(403, "无权播放")
    elif uri.startswith("/uploads/"):
        if user["role"] == "proctor":
            raise HTTPException(403, "无权上传")
        if method not in ("GET", "HEAD", "OPTIONS") and not hmac.compare_digest(user["csrf"], request.headers.get("x-csrf-token", "")):
            raise HTTPException(403, "请求校验失败")
        uid = uri.removeprefix("/uploads/").strip("/")
        if uid:
            if not re.fullmatch(r"[a-f0-9]{32}", uid):
                raise HTTPException(403)
            with db() as c:
                row = c.execute("SELECT * FROM uploads WHERE id=?", (uid,)).fetchone()
            if not row or (user["role"] != "admin" and row["owner"] != user["username"]):
                raise HTTPException(403)
            if method == "DELETE" and row["state"] != "uploading":
                raise HTTPException(403, "已完成原件不能删除")
        elif method not in ("POST", "OPTIONS"):
            raise HTTPException(403)
    else:
        raise HTTPException(403)
    return Response(status_code=204)


def hook_reject(message):
    return {"RejectUpload": True, "HTTPResponse": {"StatusCode": 403, "Body": message}}


@app.post("/internal/tus")
async def tus_hook(request: Request):
    body = await request.json()
    event = body.get("Event", {})
    upload = event.get("Upload", {})
    kind = body.get("Type")
    if kind == "pre-create":
        user = session_from_cookie(request.headers.get("cookie"))
        if not user or user["role"] == "proctor" or not hmac.compare_digest(user["csrf"], request.headers.get("x-csrf-token", "")):
            return hook_reject("Authentication required")
        meta = upload.get("MetaData") or {}
        cid, rid = meta.get("contestant_id", ""), meta.get("round_id", "")
        expected = meta.get("sha256", "").lower()
        filename = meta.get("filename", "")
        size = upload.get("Size")
        if not owns(user, cid) or not re.fullmatch(r"[0-9a-f]{64}", expected):
            return hook_reject("Invalid ownership or SHA-256")
        if not isinstance(size, int) or not 0 < size <= 32 * 1024**3 or upload.get("SizeIsDeferred") or upload.get("IsPartial") or upload.get("IsFinal"):
            return hook_reject("A fixed size of at most 32 GiB is required")
        if not filename.lower().endswith(".mkv") or len(filename) > 255 or "/" in filename or "\\" in filename:
            return hook_reject("An original MKV filename is required")
        with db(immediate=True) as c:
            if c.execute("SELECT 1 FROM rounds WHERE enabled=1 AND start_at<=? AND end_at>?", (now(), now())).fetchone():
                return hook_reject("Original uploads are paused during scheduled contest rounds")
            if not c.execute("SELECT 1 FROM contestants WHERE id=? AND disabled=0", (cid,)).fetchone() or not c.execute("SELECT 1 FROM rounds WHERE id=?", (rid,)).fetchone():
                return hook_reject("Unknown contestant or round")
            disk = storage(c)
            outstanding = c.execute("SELECT COALESCE(SUM(size),0) FROM uploads WHERE state='uploading'").fetchone()[0]
            if disk["budget_remaining_bytes"] < size or disk["available_bytes"] - outstanding < size + 100 * 1024**3 or disk["used_fraction"] >= 0.9:
                return hook_reject("Storage admission limit reached")
            uid = secrets.token_hex(16)
            c.execute("INSERT INTO uploads VALUES(?,?,?,?,?,?,?,?,?)", (uid, cid, rid, filename, size, expected, user["username"], "uploading", now()))
            audit(c, user["username"], "upload.create", uid, filename)
        return {"ChangeFileInfo": {"ID": uid, "MetaData": {"filename": filename,
                                    "contestant_id": cid, "round_id": rid, "sha256": expected}}}
    if kind == "post-finish":
        uid = upload.get("ID", "")
        with db(immediate=True) as c:
            row = c.execute("SELECT * FROM uploads WHERE id=?", (uid,)).fetchone()
            if row:
                path = "uploads/" + uid
                if safe_path(path).stat().st_size != row["size"]:
                    raise HTTPException(409, "Upload size mismatch")
                queue_media(c, contestant_id=row["contestant_id"], round_id=row["round_id"], kind="original",
                            source_uid="upload:" + uid, path=path, filename=row["filename"])
                c.execute("UPDATE uploads SET state='received' WHERE id=?", (uid,))
        return {}
    if kind == "pre-terminate":
        uid = upload.get("ID", "")
        user = session_from_cookie(request.headers.get("cookie"))
        with db(immediate=True) as c:
            row = c.execute("SELECT * FROM uploads WHERE id=?", (uid,)).fetchone()
            allowed = row and user and row["state"] == "uploading" and (user["role"] == "admin" or row["owner"] == user["username"])
            if allowed:
                c.execute("UPDATE uploads SET state='cancelled' WHERE id=?", (uid,))
                audit(c, user["username"], "upload.cancel", uid)
        return {"RejectTermination": not bool(allowed)}
    return {}


@app.post("/internal/srs")
async def srs_hook(request: Request):
    body = await request.json()
    action = body.get("action")
    cid = body.get("stream", "")
    params = parse_qs(body.get("param", "").lstrip("?"))
    token = params.get("token", [""])[0]
    if action == "on_play":
        return {"code": 0 if token and hmac.compare_digest(token, secret("PLAY_SECRET")) else 403}
    if action == "on_publish":
        if body.get("app") != "live" or not re.fullmatch(ID, cid):
            return {"code": 403}
        rid = params.get("round", [""])[0]
        with db(immediate=True) as c:
            row = c.execute("SELECT * FROM contestants WHERE id=? AND disabled=0", (cid,)).fetchone()
            valid_round = c.execute("SELECT 1 FROM rounds WHERE id=? AND enabled=1", (rid,)).fetchone()
            if not row or not row["stream_key_hash"] or not token or not hmac.compare_digest(row["stream_key_hash"], digest(token)) or not valid_round:
                return {"code": 403}
            if c.execute("SELECT 1 FROM live_sessions WHERE contestant_id=? AND ended IS NULL", (cid,)).fetchone():
                return {"code": 409}
            c.execute("INSERT INTO live_sessions(id,contestant_id,round_id,client_id,started,last_seen) VALUES(?,?,?,?,?,?)",
                      (secrets.token_hex(16), cid, rid, str(body.get("client_id", "")), now(), now()))
            resolve(c, "offline:" + cid)
            audit(c, "srs", "stream.publish", cid, rid)
        return {"code": 0}
    if action == "on_unpublish":
        with db() as c:
            row = c.execute("SELECT * FROM live_sessions WHERE contestant_id=? AND client_id=? AND ended IS NULL", (cid, str(body.get("client_id", "")))).fetchone()
            if row:
                c.execute("UPDATE live_sessions SET ended=? WHERE id=?", (now(), row["id"]))
                alert(c, "offline:" + cid, "推流已断开", contestant_id=cid)
                audit(c, "srs", "stream.unpublish", cid)
        return {"code": 0}
    if action == "on_dvr":
        raw = Path(body.get("file", ""))
        try:
            relative = str(raw.resolve().relative_to(DATA.resolve()))
            path = safe_path(relative)
            if not path.is_relative_to((DATA / "dvr").resolve()):
                raise ValueError()
        except (ValueError, OSError):
            return {"code": 403}
        with db(immediate=True) as c:
            live = c.execute("SELECT * FROM live_sessions WHERE contestant_id=? AND client_id=? ORDER BY started DESC LIMIT 1", (cid, str(body.get("client_id", "")))).fetchone()
            if not live:
                return {"code": 404}
            queue_media(c, contestant_id=cid, round_id=live["round_id"], kind="dvr", source_uid="dvr:" + relative,
                        path=relative, filename=path.name)
            resolve(c, "dvr:" + cid)
        return {"code": 0}
    return {"code": 0}


@app.get("/api/metrics")
def metrics(request: Request):
    auth = request.headers.get("authorization", "")
    if not hmac.compare_digest(auth, "Bearer " + secret("METRICS_TOKEN")):
        raise HTTPException(401)
    with db() as c:
        check_worker(c)
        disk = storage(c)
        monitor = get_state(c, "monitor", {})
        values = {"nco_storage_available_bytes": disk["available_bytes"],
                  "nco_storage_used_fraction": disk["used_fraction"],
                  "nco_storage_committed_bytes": disk["committed_bytes"],
                  "nco_storage_budget_bytes": disk["budget_bytes"],
                  "nco_srs_available": int(bool(monitor.get("srs_ok"))),
                  "nco_worker_heartbeat_age_seconds": now() - get_state(c, "worker_at", 0),
                  "nco_active_streams": c.execute("SELECT COUNT(*) FROM live_sessions WHERE ended IS NULL").fetchone()[0],
                  "nco_open_alerts": c.execute("SELECT COUNT(*) FROM alerts WHERE resolved IS NULL").fetchone()[0]}
        output = "".join(f"# TYPE {key} gauge\n{key} {value}\n" for key, value in values.items())
        for row in c.execute("SELECT state,COUNT(*) n FROM jobs GROUP BY state"):
            output += f'nco_jobs{{state="{row["state"]}"}} {row["n"]}\n'
    return Response(output, media_type="text/plain; version=0.0.4")
