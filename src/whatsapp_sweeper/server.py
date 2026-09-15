#!/usr/bin/env python3
"""WhatsApp Sweeper: find and delete WhatsApp Desktop's bloated media cache.

Run: whatsapp-sweeper
Then open http://localhost:8765 (opened automatically).
"""
import hashlib
import http.server
import json
import os
import socketserver
import sqlite3
import subprocess
import sys
import tempfile
import threading
import urllib.parse
import webbrowser
from pathlib import Path

from PIL import Image

MEDIA_DIR = Path(os.path.expanduser(
    "~/Library/Group Containers/group.net.whatsapp.WhatsApp.shared/Message/Media"
))
DB_PATH = os.path.expanduser(
    "~/Library/Group Containers/group.net.whatsapp.WhatsApp.shared/ChatStorage.sqlite"
)
STATIC_DIR = Path(__file__).parent
PORT = 8765
SIDECAR_EXTS = {".thumb", ".favicon", ".mmsthumb"}
APPLE_EPOCH_OFFSET = 978307200  # 2001-01-01 -> unix epoch

IMAGE_EXTS = {"jpg", "jpeg", "png", "webp", "gif", "bmp", "tiff"}
VIDEO_EXTS = {"mp4", "mov", "m4v", "3gp", "avi", "mkv"}
THUMB_SIZE = 320
THUMB_CACHE_DIR = Path(tempfile.gettempdir()) / "whatsapp-sweeper-thumbs"
THUMB_CACHE_DIR.mkdir(exist_ok=True)

RECORDS = {}   # id -> record dict, mutated on delete
ORDER = []     # ids sorted by size desc, fixed at startup

DB_QUERY = """
    SELECT
        mi.ZMEDIALOCALPATH AS path,
        mi.ZMOVIEDURATION AS duration,
        m.ZMESSAGEDATE AS msg_date,
        m.ZISFROMME AS is_from_me,
        m.ZFROMJID AS from_jid,
        gm.ZMEMBERJID AS member_jid,
        pn_member.ZPUSHNAME AS member_pushname,
        pn_direct.ZPUSHNAME AS direct_pushname,
        cs.ZPARTNERNAME AS chat_name,
        cs.ZSESSIONTYPE AS session_type
    FROM ZWAMEDIAITEM mi
    JOIN ZWAMESSAGE m ON m.Z_PK = mi.ZMESSAGE
    LEFT JOIN ZWAGROUPMEMBER gm ON gm.Z_PK = m.ZGROUPMEMBER
    LEFT JOIN ZWAPROFILEPUSHNAME pn_member ON pn_member.ZJID = gm.ZMEMBERJID
    LEFT JOIN ZWAPROFILEPUSHNAME pn_direct ON pn_direct.ZJID = m.ZFROMJID
    JOIN ZWACHATSESSION cs ON cs.Z_PK = m.ZCHATSESSION
    WHERE mi.ZMEDIALOCALPATH IS NOT NULL
"""


def load_db_index():
    """rel path (inside MEDIA_DIR) -> {sender, chat, duration, date}"""
    index = {}
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    for row in conn.execute(DB_QUERY):
        rel = row["path"]
        if rel.startswith("Media/"):
            rel = rel[len("Media/"):]

        if row["is_from_me"]:
            sender = "Me"
        elif row["member_pushname"]:
            sender = row["member_pushname"]
        elif row["direct_pushname"]:
            sender = row["direct_pushname"]
        elif row["session_type"] == 0 and row["chat_name"]:
            sender = row["chat_name"]
        else:
            jid = row["member_jid"] or row["from_jid"] or ""
            sender = jid.split("@")[0] if jid else "Unknown"

        date = None
        if row["msg_date"] is not None:
            date = row["msg_date"] + APPLE_EPOCH_OFFSET

        index[rel] = {
            "sender": sender or "Unknown",
            "chat": row["chat_name"] or sender or "Unknown",
            "duration": row["duration"],
            "date": date,
        }
    conn.close()
    return index


def scan_media():
    db_index = load_db_index()
    next_id = 0
    for root, _dirs, files in os.walk(MEDIA_DIR):
        for name in files:
            if name.startswith("."):
                continue
            ext = os.path.splitext(name)[1].lower()
            if ext in SIDECAR_EXTS:
                continue
            full = Path(root) / name
            rel = str(full.relative_to(MEDIA_DIR))
            try:
                st = full.stat()
            except OSError:
                continue
            meta = db_index.get(rel)
            ext_lower = ext.lstrip(".").lower()
            is_video = ext_lower in VIDEO_EXTS
            has_sidecar_thumb = os.path.exists(os.path.splitext(full)[0] + ".thumb")
            RECORDS[next_id] = {
                "id": next_id,
                "path": str(full),
                "name": name,
                "ext": ext_lower,
                "size": st.st_size,
                "date": meta["date"] if meta and meta["date"] else st.st_mtime,
                "duration": meta["duration"] if meta and is_video else None,
                "sender": meta["sender"] if meta else "Unknown",
                "chat": meta["chat"] if meta else "Unknown",
                "has_thumb": ext_lower in IMAGE_EXTS or is_video or has_sidecar_thumb,
            }
            next_id += 1
    ORDER[:] = sorted(RECORDS.keys(), key=lambda i: RECORDS[i]["size"], reverse=True)
    total_size = sum(r["size"] for r in RECORDS.values())
    print(f"Indexed {len(RECORDS)} files, {total_size / (1024**3):.1f} GB")


def thumb_cache_path(path):
    key = hashlib.sha1(path.encode()).hexdigest()
    return THUMB_CACHE_DIR / f"{key}.jpg"


def get_or_make_thumb(rec):
    """Real thumbnail generated from the source file (cached to disk),
    falling back to WhatsApp's own tiny cached .thumb sidecar on failure."""
    cache_path = thumb_cache_path(rec["path"])
    if cache_path.exists():
        return cache_path

    ext = rec["ext"]
    try:
        if ext in IMAGE_EXTS:
            with Image.open(rec["path"]) as im:
                im = im.convert("RGB")
                im.thumbnail((THUMB_SIZE, THUMB_SIZE))
                im.save(cache_path, "JPEG", quality=85)
            return cache_path
        elif ext in VIDEO_EXTS:
            result = subprocess.run(
                [
                    "ffmpeg", "-y", "-ss", "0.5", "-i", rec["path"],
                    "-frames:v", "1", "-vf", f"scale={THUMB_SIZE}:-1",
                    str(cache_path),
                ],
                capture_output=True, timeout=15,
            )
            if result.returncode == 0 and cache_path.exists():
                return cache_path
    except Exception as exc:
        print(f"thumb generation failed for {rec['path']}: {exc}")

    legacy = os.path.splitext(rec["path"])[0] + ".thumb"
    return Path(legacy) if os.path.exists(legacy) else None


def compute_agg(key):
    agg = {}
    for r in RECORDS.values():
        s = agg.setdefault(r[key], {"name": r[key], "count": 0, "total_size": 0})
        s["count"] += 1
        s["total_size"] += r["size"]
    return sorted(agg.values(), key=lambda s: s["total_size"], reverse=True)


def compute_senders():
    return compute_agg("sender")


def compute_chats():
    return compute_agg("chat")


def filter_ids(include, exclude, include_chat, exclude_chat):
    for i in ORDER:
        r = RECORDS.get(i)
        if r is None:
            continue
        if include and r["sender"] not in include:
            continue
        if r["sender"] in exclude:
            continue
        if include_chat and r["chat"] not in include_chat:
            continue
        if r["chat"] in exclude_chat:
            continue
        yield i, r


def item_json(i, r):
    return {
        "id": i,
        "name": r["name"],
        "ext": r["ext"],
        "size": r["size"],
        "date": r["date"],
        "duration": r["duration"],
        "sender": r["sender"],
        "chat": r["chat"],
        "has_thumb": r["has_thumb"],
    }


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def _send_json(self, obj, status=200):
        body = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _serve_bytes(self, data, content_type):
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/":
            try:
                data = (STATIC_DIR / "index.html").read_bytes()
            except OSError:
                self.send_error(404)
                return
            self._serve_bytes(data, "text/html; charset=utf-8")
        elif parsed.path == "/api/items":
            self._api_items(parsed)
        elif parsed.path == "/api/senders":
            self._send_json(compute_senders())
        elif parsed.path == "/api/chats":
            self._send_json(compute_chats())
        elif parsed.path == "/api/select_all":
            self._api_select_all(parsed)
        elif parsed.path.startswith("/thumb/"):
            self._serve_thumb(parsed.path[len("/thumb/"):])
        else:
            self.send_error(404)

    @staticmethod
    def _parse_filters(qs):
        include = {s for s in qs.get("include", [""])[0].split(",") if s}
        exclude = {s for s in qs.get("exclude", [""])[0].split(",") if s}
        include_chat = {s for s in qs.get("include_chat", [""])[0].split(",") if s}
        exclude_chat = {s for s in qs.get("exclude_chat", [""])[0].split(",") if s}
        return include, exclude, include_chat, exclude_chat

    def _api_items(self, parsed):
        qs = urllib.parse.parse_qs(parsed.query)
        try:
            offset = int(qs.get("offset", ["0"])[0])
            limit = min(int(qs.get("limit", ["60"])[0]), 200)
        except ValueError:
            offset, limit = 0, 60

        matched = list(filter_ids(*self._parse_filters(qs)))
        total_count = len(matched)
        total_size = sum(r["size"] for _, r in matched)
        page = matched[offset: offset + limit]
        next_offset = offset + limit if offset + limit < total_count else None
        self._send_json({
            "items": [item_json(i, r) for i, r in page],
            "total_count": total_count,
            "total_size": total_size,
            "next_offset": next_offset,
        })

    def _api_select_all(self, parsed):
        qs = urllib.parse.parse_qs(parsed.query)
        matched = list(filter_ids(*self._parse_filters(qs)))
        self._send_json({
            "ids": [i for i, _r in matched],
            "total_count": len(matched),
            "total_size": sum(r["size"] for _, r in matched),
        })

    def _serve_thumb(self, id_str):
        try:
            rec = RECORDS[int(id_str)]
        except (KeyError, ValueError):
            self.send_error(404)
            return
        thumb_path = get_or_make_thumb(rec)
        if thumb_path is None:
            self.send_error(404)
            return
        try:
            data = thumb_path.read_bytes()
        except OSError:
            self.send_error(404)
            return
        self._serve_bytes(data, "image/jpeg")

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        length = int(self.headers.get("Content-Length", 0) or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            payload = json.loads(raw or b"{}")
        except json.JSONDecodeError:
            payload = {}

        if parsed.path == "/api/open":
            self._api_open(payload)
        elif parsed.path == "/api/delete":
            self._api_delete(payload)
        else:
            self.send_error(404)

    def _api_open(self, payload):
        rec = RECORDS.get(payload.get("id"))
        if rec is None:
            self._send_json({"ok": False, "error": "not found"}, 404)
            return
        subprocess.run(["open", rec["path"]])
        self._send_json({"ok": True})

    def _api_delete(self, payload):
        ids = payload.get("ids", [])
        results = {}
        for id_ in ids:
            rec = RECORDS.get(id_)
            if rec is None:
                results[str(id_)] = False
                continue
            try:
                os.remove(rec["path"])
            except OSError as exc:
                results[str(id_)] = False
                print(f"delete failed for {rec['path']}: {exc}")
                continue
            base = os.path.splitext(rec["path"])[0]
            for ext in SIDECAR_EXTS:
                try:
                    os.remove(base + ext)
                except OSError:
                    pass
            del RECORDS[id_]
            results[str(id_)] = True
        self._send_json({"results": results})


def main():
    if sys.platform != "darwin":
        sys.exit(
            "WhatsApp Sweeper only supports macOS (it reads WhatsApp Desktop's "
            "local media cache and chat database, which only exist there)."
        )
    if not MEDIA_DIR.exists():
        sys.exit(
            f"Couldn't find WhatsApp Desktop's media folder at:\n  {MEDIA_DIR}\n"
            "Is WhatsApp Desktop installed, and have you opened it at least once?"
        )
    if not os.path.exists(DB_PATH):
        sys.exit(f"Couldn't find WhatsApp's chat database at:\n  {DB_PATH}")

    scan_media()
    with socketserver.ThreadingTCPServer(("127.0.0.1", PORT), Handler) as httpd:
        httpd.allow_reuse_address = True
        url = f"http://localhost:{PORT}"
        print(f"Serving on {url}")
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()
