from contextlib import contextmanager
import base64
import hmac
import json
import os
import re
import sqlite3
import threading
import time
from urllib.parse import quote
from pathlib import Path

from flask import Flask, Response, abort, jsonify, request, send_file
from werkzeug.exceptions import HTTPException

ROOT = Path(__file__).resolve().parent
DATA = Path(os.environ.get("DATA_DIR", ROOT / "storage"))
DATA.mkdir(parents=True, exist_ok=True)
KEY = quote(os.environ["EDIT_KEY"], safe="~()*!.'-_").encode()
if not KEY:
    raise RuntimeError("Set EDIT_KEY before starting")
app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 128 * 1024 * 1024
changed = threading.Condition()
revision = 0

@contextmanager
def connect():
    db = sqlite3.connect(DATA / "catalog.sqlite3", timeout=20)
    db.row_factory = sqlite3.Row
    try:
        with db:
            yield db
    finally:
        db.close()

def initialize():
    with connect() as db:
        db.execute("CREATE TABLE IF NOT EXISTS entries(id TEXT PRIMARY KEY, meta TEXT, body BLOB)")
        db.execute("CREATE TABLE IF NOT EXISTS settings(k TEXT PRIMARY KEY)")
        if db.execute("SELECT 1 FROM settings WHERE k='seeded'").fetchone():
            return
        html = (ROOT / "seed.html").read_text(encoding="utf-8-sig")
        seed = re.search(r'<script type="application/json" id="bundle">(.+?)</script>', html, re.S)
        for x in json.loads(seed.group(1)):
            body = base64.b64decode(x.pop("data")) if "data" in x else None
            x["version"] = 1
            db.execute("INSERT INTO entries VALUES(?,?,?)",(x["id"],json.dumps(x),body))
        db.execute("INSERT INTO settings VALUES('seeded')")
initialize()

@app.errorhandler(HTTPException)
def error(e):
    return jsonify(error=e.description), e.code

@app.get("/")
def home():
    return send_file(ROOT / "index.html", max_age=0)

@app.get("/api/catalog")
def catalog():
    with connect() as db:
        rows = [json.loads(r[0]) for r in db.execute("SELECT meta FROM entries ORDER BY rowid DESC")]
    return jsonify(rows), {"Cache-Control":"no-store"}

@app.get("/api/file/<key>")
def file(key):
    with connect() as db:
        row = db.execute("SELECT body FROM entries WHERE id=?",(key,)).fetchone()
    if not row or row[0] is None:
        abort(404)
    return Response(row[0], mimetype="application/octet-stream", headers={"Content-Disposition":"attachment","X-Content-Type-Options":"nosniff","Cache-Control":"no-store"})

@app.get("/api/events")
def events():
    def stream():
        seen = -1
        while True:
            with changed:
                changed.wait_for(lambda: seen != revision, timeout=15)
                tick = seen != revision
                seen = revision
            yield "data: update\n\n" if tick else ": heartbeat\n\n"
    return Response(stream(),mimetype="text/event-stream",headers={"Cache-Control":"no-store","X-Accel-Buffering":"no"})

@app.post("/api/change")
def write():
    global revision
    if not hmac.compare_digest(request.headers.get("Authorization","").encode(), b"Bearer "+KEY):
        abort(403, "编辑口令有误")
    fields=request.form
    key=fields.get("id","")
    if not key or len(key)>100:
        abort(400)
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        row=db.execute("SELECT meta FROM entries WHERE id=?",(key,)).fetchone()
        old=json.loads(row[0]) if row else {}
        if fields.get("version","0")!=str(old.get("version",0)):
            abort(409,"作品已被其他人修改，请重新打开最新条目。")
        if fields.get("op")=="delete":
            if not row:
                abort(404)
            if old.get("kind")=="folder":
                for child in db.execute("SELECT id,meta FROM entries").fetchall():
                    item=json.loads(child["meta"])
                    if item.get("folder")==key:
                        item.update(folder="",version=item["version"]+1)
                        db.execute("UPDATE entries SET meta=? WHERE id=?",(json.dumps(item),child["id"]))
            db.execute("DELETE FROM entries WHERE id=?",(key,))
        else:
            title=fields.get("title","").strip()
            if not title:
                abort(400,"请填写标题")
            kind=old.get("kind","file") if row else fields.get("kind","file")
            item=dict(old,id=key,kind=kind,title=title,version=old.get("version",0)+1,updated=int(time.time()*1000))
            upload=request.files.get("file")
            if kind!="folder":
                folder=fields.get("folder","")
                target=db.execute("SELECT meta FROM entries WHERE id=?",(folder,)).fetchone()
                if folder and (not target or json.loads(target[0]).get("kind")!="folder"):
                    abort(409,"目标文件夹已被删除")
                item.update(folder=folder,author=fields.get("author","").strip())
                if upload is not None:
                    item.update(name=upload.filename,type=upload.mimetype or "application/octet-stream")
                elif not row:
                    abort(400,"请选择文件")
            meta=json.dumps(item)
            if not row:
                db.execute("INSERT INTO entries VALUES(?,?,?)",(key,meta,upload.read() if upload else None))
            elif upload:
                db.execute("UPDATE entries SET meta=?,body=? WHERE id=?",(meta,upload.read(),key))
            else:
                db.execute("UPDATE entries SET meta=? WHERE id=?",(meta,key))
    with changed:
        revision+=1
        changed.notify_all()
    return "",204

if __name__=="__main__":
    app.run(port=int(os.environ.get("PORT","8765")),threaded=True)
