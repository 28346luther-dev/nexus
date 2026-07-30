#!/usr/bin/env python3
"""
Nexus — a Discord-style messaging site.

Zero dependencies: Python standard library + SQLite only.

    python3 app.py           # http://localhost:8000
    python3 app.py --port 9000
"""

import argparse
import json
import mimetypes
import os
import re
import secrets
import select
import socket
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs, unquote

import db
import images

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(BASE_DIR, "static")

MAX_BODY = 256 * 1024
MAX_MESSAGE = 2000
POLL_TIMEOUT = 20.0      # seconds a long-poll may hang
POLL_INTERVAL = 0.4

# Filled in by main() so invite links can name an address other people can open.
SERVER_PORT = 8000


def lan_ip():
    """This machine's address on the local network, for shareable invite links.

    Opening a UDP socket sends no packets; it just asks the OS which interface
    would be used to reach the outside world.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("192.0.2.1", 1))   # TEST-NET-1: reserved, never routed
        return sock.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        sock.close()


class HttpError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status
        self.message = message


class ClientGone(Exception):
    """The browser hung up mid-request; stop work and close quietly."""


# ------------------------------------------------------------------ routing

ROUTES = []


def route(method, pattern, raw=0):
    """Register a handler. `raw` = byte limit for routes that take a file body.

    Image uploads send the file as the entire request body rather than as
    multipart form data — the stdlib lost its multipart parser when the cgi
    module was removed in Python 3.13, and raw bodies are simpler anyway.
    """
    compiled = re.compile("^" + pattern + "$")

    def deco(fn):
        ROUTES.append((method, compiled, fn, raw))
        return fn

    return deco


class Req:
    """Everything a handler needs: db connection, body, query, current user."""

    def __init__(self, conn, body, query, cookies, user, handler=None, raw=b""):
        self.conn = conn
        self.body = body
        self.query = query
        self.cookies = cookies
        self.user = user
        self.handler = handler
        self.raw = raw
        self.set_cookie = None
        self.clear_cookie = False

    def client_gone(self):
        """True once the browser has hung up (e.g. an aborted long-poll)."""
        if not self.handler:
            return False
        sock = self.handler.connection
        try:
            readable, _, _ = select.select([sock], [], [], 0)
            if not readable:
                return False
            return sock.recv(1, socket.MSG_PEEK) == b""
        except OSError:
            return True

    def field(self, name, required=True, maxlen=None, default=None):
        value = self.body.get(name, default)
        if isinstance(value, str):
            value = value.strip()
        if required and not value:
            raise HttpError(400, f"'{name}' is required.")
        if maxlen and isinstance(value, str) and len(value) > maxlen:
            raise HttpError(400, f"'{name}' must be {maxlen} characters or fewer.")
        return value

    def require_auth(self):
        if not self.user:
            raise HttpError(401, "You need to sign in.")
        return self.user

    # -- reverse-proxy awareness (Railway and friends terminate TLS upstream)

    def forwarded_proto(self):
        if not self.handler:
            return "http"
        raw = self.handler.headers.get("X-Forwarded-Proto") or ""
        proto = raw.split(",")[0].strip().lower()
        return proto if proto in ("http", "https") else "http"

    def public_origin(self):
        """The origin as the outside world sees it, or None if unknowable."""
        if not self.handler:
            return None
        headers = self.handler.headers
        host = (headers.get("X-Forwarded-Host") or headers.get("Host") or "")
        host = host.split(",")[0].strip()
        return f"{self.forwarded_proto()}://{host}" if host else None


# ------------------------------------------------------------------ helpers

def guild_member_or_403(req, guild_id):
    row = req.conn.execute(
        "SELECT 1 FROM guild_members WHERE guild_id = ? AND user_id = ?",
        (guild_id, req.user["id"]),
    ).fetchone()
    if not row:
        raise HttpError(403, "You are not a member of that server.")


def channel_access_or_403(req, channel_id):
    """Return the channel row if the current user may read/write it."""
    ch = req.conn.execute("SELECT * FROM channels WHERE id = ?", (channel_id,)).fetchone()
    if not ch:
        raise HttpError(404, "Channel not found.")
    if ch["kind"] == "dm":
        ok = req.conn.execute(
            "SELECT 1 FROM dm_participants WHERE channel_id = ? AND user_id = ?",
            (channel_id, req.user["id"]),
        ).fetchone()
        if not ok:
            raise HttpError(403, "That conversation is not yours.")
    else:
        guild_member_or_403(req, ch["guild_id"])
    return ch


def serialize_messages(conn, rows, user_id):
    """Serialize messages, batching attachments/reactions to avoid N+1 queries."""
    out = [serialize_message(r) for r in rows]
    if not out:
        return out
    by_id = {m["id"]: m for m in out}
    ids = list(by_id)
    marks = ",".join("?" * len(ids))

    for a in conn.execute(
        f"SELECT * FROM attachments WHERE message_id IN ({marks}) ORDER BY id", ids
    ):
        by_id[a["message_id"]]["attachments"].append({
            "id": a["id"],
            "url": f"/uploads/{a['stored_name']}",
            "name": a["original_name"],
            "mime": a["mime"],
            "size": a["size"],
            "width": a["width"],
            "height": a["height"],
        })

    for r in conn.execute(
        f"SELECT message_id, emoji, COUNT(*) c,"
        f" SUM(CASE WHEN user_id = ? THEN 1 ELSE 0 END) mine"
        f" FROM reactions WHERE message_id IN ({marks})"
        f" GROUP BY message_id, emoji ORDER BY MIN(id)",
        [user_id] + ids,
    ):
        by_id[r["message_id"]]["reactions"].append({
            "emoji": r["emoji"],
            "count": r["c"],
            "me": bool(r["mine"]),
        })

    sticker_ids = [m["stickerId"] for m in out if m["stickerId"]]
    if sticker_ids:
        smarks = ",".join("?" * len(sticker_ids))
        stickers = {
            s["id"]: s
            for s in conn.execute(
                f"SELECT * FROM stickers WHERE id IN ({smarks})", sticker_ids
            )
        }
        for m in out:
            s = stickers.get(m["stickerId"])
            if s:
                m["sticker"] = {
                    "id": s["id"],
                    "name": s["name"],
                    "url": f"/uploads/{s['stored_name']}",
                }
    return out


def serialize_one(conn, message_id, user_id):
    row = conn.execute(MESSAGE_SELECT + " WHERE m.id = ?", (message_id,)).fetchone()
    return serialize_messages(conn, [row], user_id)[0]


def serialize_message(row):
    keys = row.keys()
    return {
        "id": row["id"],
        "channelId": row["channel_id"],
        "content": row["content"],
        "createdAt": row["created_at"],
        "editedAt": row["edited_at"],
        "stickerId": row["sticker_id"] if "sticker_id" in keys else None,
        "sticker": None,
        "attachments": [],
        "reactions": [],
        "author": {
            "id": row["author_id"],
            "username": row["username"],
            "discriminator": row["discriminator"],
            "tag": f'{row["username"]}#{row["discriminator"]}',
            "color": row["color"],
            "avatarUrl": f"/uploads/{row['avatar']}" if row["avatar"] else None,
        },
    }


MESSAGE_SELECT = """
SELECT m.*, u.username, u.discriminator, u.color, u.avatar
FROM messages m JOIN users u ON u.id = m.author_id
"""


def dm_partner(conn, channel_id, user_id):
    return conn.execute(
        "SELECT u.* FROM dm_participants p JOIN users u ON u.id = p.user_id"
        " WHERE p.channel_id = ? AND p.user_id != ?",
        (channel_id, user_id),
    ).fetchone()


def unread_count(conn, user_id, channel_id):
    return conn.execute(
        "SELECT COUNT(*) c FROM messages m"
        " LEFT JOIN read_state r ON r.user_id = ? AND r.channel_id = m.channel_id"
        " WHERE m.channel_id = ? AND m.author_id != ?"
        "   AND m.id > COALESCE(r.last_read_msg, 0)",
        (user_id, channel_id, user_id),
    ).fetchone()["c"]


def revision(conn, user_id):
    """Cheap fingerprint of everything that could change this user's sidebar."""
    parts = [
        conn.execute(
            "SELECT COALESCE(MAX(m.id), 0) v FROM messages m"
            " LEFT JOIN dm_participants p ON p.channel_id = m.channel_id"
            " LEFT JOIN guild_members g ON g.guild_id ="
            "   (SELECT guild_id FROM channels WHERE id = m.channel_id)"
            " WHERE p.user_id = ? OR g.user_id = ?",
            (user_id, user_id),
        ).fetchone()["v"],
        conn.execute(
            "SELECT COALESCE(MAX(id), 0) v FROM friendships"
            " WHERE requester_id = ? OR addressee_id = ?",
            (user_id, user_id),
        ).fetchone()["v"],
        conn.execute(
            "SELECT COUNT(*) v FROM friendships"
            " WHERE requester_id = ? OR addressee_id = ?",
            (user_id, user_id),
        ).fetchone()["v"],
        conn.execute(
            "SELECT COALESCE(MAX(id), 0) v FROM guild_members WHERE user_id = ?",
            (user_id,),
        ).fetchone()["v"],
        conn.execute(
            "SELECT COALESCE(MAX(c.id), 0) v FROM channels c"
            " JOIN guild_members g ON g.guild_id = c.guild_id AND g.user_id = ?",
            (user_id,),
        ).fetchone()["v"],
        conn.execute(
            "SELECT COALESCE(SUM(last_read_msg), 0) v FROM read_state WHERE user_id = ?",
            (user_id,),
        ).fetchone()["v"],
    ]
    return "-".join(str(p) for p in parts)


def channel_revision(conn, channel_id):
    """Fingerprint of one channel's visible content.

    Covers edits, deletions and reactions — none of which create a new message,
    so the message stream alone would never tell the client to redraw.
    """
    m = conn.execute(
        "SELECT COALESCE(MAX(id), 0) a, COUNT(*) b, COALESCE(MAX(edited_at), 0) c"
        " FROM messages WHERE channel_id = ?",
        (channel_id,),
    ).fetchone()
    r = conn.execute(
        "SELECT COALESCE(MAX(rx.id), 0) a, COUNT(*) b FROM reactions rx"
        " JOIN messages m ON m.id = rx.message_id WHERE m.channel_id = ?",
        (channel_id,),
    ).fetchone()
    return f"{m['a']}.{m['b']}.{m['c']}-{r['a']}.{r['b']}"


# ------------------------------------------------------------------ auth API

@route("POST", r"/api/register")
def api_register(req):
    username = req.field("username", maxlen=32)
    email = req.field("email", maxlen=254)
    password = req.field("password", maxlen=200)

    if not db.USERNAME_RE.match(username):
        raise HttpError(400, "Usernames are 2–32 characters: letters, numbers, spaces, . _ -")
    if not db.EMAIL_RE.match(email):
        raise HttpError(400, "That email address doesn't look valid.")
    if len(password) < 8:
        raise HttpError(400, "Passwords must be at least 8 characters.")

    exists = req.conn.execute(
        "SELECT 1 FROM users WHERE email = ?", (email.lower(),)
    ).fetchone()
    if exists:
        raise HttpError(409, "An account with that email already exists.")

    with req.conn:
        user_id = db.create_user(req.conn, username, email, password)
    return start_session(req, user_id)


@route("POST", r"/api/login")
def api_login(req):
    email = req.field("email", maxlen=254).lower()
    password = req.field("password", maxlen=200)
    row = req.conn.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
    if not row or not db.verify_password(password, row["salt"], row["password_hash"]):
        raise HttpError(401, "Incorrect email or password.")
    return start_session(req, row["id"])


def start_session(req, user_id):
    token = secrets.token_urlsafe(32)
    with req.conn:
        req.conn.execute(
            "INSERT INTO sessions (token, user_id, created_at, last_used) VALUES (?,?,?,?)",
            (token, user_id, db.now(), db.now()),
        )
        req.conn.execute("UPDATE users SET last_seen = ? WHERE id = ?", (db.now(), user_id))
    req.set_cookie = token
    row = req.conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    return {"user": me_payload(row)}


@route("POST", r"/api/logout")
def api_logout(req):
    token = req.cookies.get("session")
    if token:
        with req.conn:
            req.conn.execute("DELETE FROM sessions WHERE token = ?", (token,))
    req.clear_cookie = True
    return {"ok": True}


def me_payload(row):
    return {
        "id": row["id"],
        "username": row["username"],
        "discriminator": row["discriminator"],
        "tag": f'{row["username"]}#{row["discriminator"]}',
        "email": row["email"],
        "color": row["color"],
        "bio": row["bio"],
        "avatarUrl": db.avatar_url(row),
    }


@route("GET", r"/api/me")
def api_me(req):
    if not req.user:
        return {"user": None}
    row = req.conn.execute("SELECT * FROM users WHERE id = ?", (req.user["id"],)).fetchone()
    return {"user": me_payload(row)}


@route("PATCH", r"/api/me")
def api_update_me(req):
    req.require_auth()
    fields, values = [], []
    if "bio" in req.body:
        fields.append("bio = ?")
        values.append(str(req.body["bio"])[:190])
    if "color" in req.body:
        color = str(req.body["color"])
        if not re.match(r"^#[0-9a-fA-F]{6}$", color):
            raise HttpError(400, "Colour must be a hex value like #5865f2.")
        fields.append("color = ?")
        values.append(color)
    if "username" in req.body:
        username = str(req.body["username"]).strip()
        if not db.USERNAME_RE.match(username):
            raise HttpError(400, "Usernames are 2–32 characters: letters, numbers, spaces, . _ -")
        tag = db.pick_discriminator(req.conn, username)
        if tag is None:
            raise HttpError(409, "That username is full — pick another.")
        current = req.conn.execute(
            "SELECT username, discriminator FROM users WHERE id = ?", (req.user["id"],)
        ).fetchone()
        if current["username"] != username:
            fields += ["username = ?", "discriminator = ?"]
            values += [username, tag]
    if fields:
        values.append(req.user["id"])
        with req.conn:
            req.conn.execute(f"UPDATE users SET {', '.join(fields)} WHERE id = ?", values)
    row = req.conn.execute("SELECT * FROM users WHERE id = ?", (req.user["id"],)).fetchone()
    return {"user": me_payload(row)}


# ---------------------------------------------------------------- guild API

@route("GET", r"/api/guilds")
def api_guilds(req):
    req.require_auth()
    uid = req.user["id"]
    guilds = []
    rows = req.conn.execute(
        "SELECT g.* FROM guilds g JOIN guild_members m ON m.guild_id = g.id"
        " WHERE m.user_id = ? ORDER BY m.joined_at",
        (uid,),
    ).fetchall()
    for g in rows:
        channels = req.conn.execute(
            "SELECT * FROM channels WHERE guild_id = ? AND kind = 'text' ORDER BY id",
            (g["id"],),
        ).fetchall()
        guilds.append(
            {
                "id": g["id"],
                "name": g["name"],
                "color": g["color"],
                "ownerId": g["owner_id"],
                "isOwner": g["owner_id"] == uid,
                "memberCount": req.conn.execute(
                    "SELECT COUNT(*) c FROM guild_members WHERE guild_id = ?", (g["id"],)
                ).fetchone()["c"],
                "channels": [
                    {
                        "id": c["id"],
                        "name": c["name"],
                        "topic": c["topic"],
                        "unread": unread_count(req.conn, uid, c["id"]),
                    }
                    for c in channels
                ],
            }
        )
    return {"guilds": guilds}


@route("POST", r"/api/guilds")
def api_create_guild(req):
    req.require_auth()
    name = req.field("name", maxlen=60)
    ts = db.now()
    with req.conn:
        cur = req.conn.execute(
            "INSERT INTO guilds (name, owner_id, color, created_at) VALUES (?,?,?,?)",
            (name, req.user["id"], secrets.choice(db.COLORS), ts),
        )
        gid = cur.lastrowid
        req.conn.execute(
            "INSERT INTO guild_members (guild_id, user_id, joined_at) VALUES (?,?,?)",
            (gid, req.user["id"], ts),
        )
        for chan in ("general", "random"):
            req.conn.execute(
                "INSERT INTO channels (guild_id, kind, name, created_at) VALUES (?,'text',?,?)",
                (gid, chan, ts),
            )
    first = req.conn.execute(
        "SELECT id FROM channels WHERE guild_id = ? ORDER BY id LIMIT 1", (gid,)
    ).fetchone()
    return {"guildId": gid, "channelId": first["id"]}


@route("PATCH", r"/api/guilds/(\d+)")
def api_rename_guild(req, guild_id):
    req.require_auth()
    guild_id = int(guild_id)
    owner_only(req, guild_id)
    name = req.field("name", maxlen=60)
    with req.conn:
        req.conn.execute("UPDATE guilds SET name = ? WHERE id = ?", (name, guild_id))
    return {"ok": True}


def owner_only(req, guild_id):
    g = req.conn.execute("SELECT * FROM guilds WHERE id = ?", (guild_id,)).fetchone()
    if not g:
        raise HttpError(404, "Server not found.")
    if g["owner_id"] != req.user["id"]:
        raise HttpError(403, "Only the server owner can do that.")
    return g


@route("DELETE", r"/api/guilds/(\d+)")
def api_delete_guild(req, guild_id):
    req.require_auth()
    guild_id = int(guild_id)
    g = req.conn.execute("SELECT * FROM guilds WHERE id = ?", (guild_id,)).fetchone()
    if not g:
        raise HttpError(404, "Server not found.")
    with req.conn:
        if g["owner_id"] == req.user["id"]:
            req.conn.execute("DELETE FROM guilds WHERE id = ?", (guild_id,))
        else:
            guild_member_or_403(req, guild_id)
            req.conn.execute(
                "DELETE FROM guild_members WHERE guild_id = ? AND user_id = ?",
                (guild_id, req.user["id"]),
            )
    return {"ok": True}


@route("POST", r"/api/guilds/(\d+)/channels")
def api_create_channel(req, guild_id):
    req.require_auth()
    guild_id = int(guild_id)
    owner_only(req, guild_id)
    name = req.field("name", maxlen=40).lower().replace(" ", "-")
    name = re.sub(r"[^a-z0-9\-_]", "", name)
    if not name:
        raise HttpError(400, "Channel names need at least one letter or number.")
    with req.conn:
        cur = req.conn.execute(
            "INSERT INTO channels (guild_id, kind, name, created_at) VALUES (?,'text',?,?)",
            (guild_id, name, db.now()),
        )
    return {"channelId": cur.lastrowid}


@route("DELETE", r"/api/channels/(\d+)")
def api_delete_channel(req, channel_id):
    req.require_auth()
    ch = req.conn.execute(
        "SELECT * FROM channels WHERE id = ?", (int(channel_id),)
    ).fetchone()
    if not ch or ch["kind"] != "text":
        raise HttpError(404, "Channel not found.")
    owner_only(req, ch["guild_id"])
    remaining = req.conn.execute(
        "SELECT COUNT(*) c FROM channels WHERE guild_id = ? AND kind = 'text'",
        (ch["guild_id"],),
    ).fetchone()["c"]
    if remaining <= 1:
        raise HttpError(400, "A server needs at least one channel.")
    with req.conn:
        req.conn.execute("DELETE FROM channels WHERE id = ?", (ch["id"],))
    return {"ok": True}


@route("GET", r"/api/guilds/(\d+)/members")
def api_guild_members(req, guild_id):
    req.require_auth()
    guild_id = int(guild_id)
    guild_member_or_403(req, guild_id)
    g = req.conn.execute("SELECT * FROM guilds WHERE id = ?", (guild_id,)).fetchone()
    rows = req.conn.execute(
        "SELECT u.* FROM guild_members m JOIN users u ON u.id = m.user_id"
        " WHERE m.guild_id = ? ORDER BY u.username COLLATE NOCASE",
        (guild_id,),
    ).fetchall()
    members = []
    for r in rows:
        item = db.public_user(r, db.is_online(r))
        item["isOwner"] = r["id"] == g["owner_id"]
        members.append(item)
    members.sort(key=lambda m: (not m["online"], not m["isOwner"], m["username"].lower()))
    return {"members": members}


@route("DELETE", r"/api/guilds/(\d+)/members/(\d+)")
def api_kick_member(req, guild_id, user_id):
    req.require_auth()
    guild_id, user_id = int(guild_id), int(user_id)
    owner_only(req, guild_id)
    if user_id == req.user["id"]:
        raise HttpError(400, "You can't remove yourself — delete the server instead.")
    with req.conn:
        req.conn.execute(
            "DELETE FROM guild_members WHERE guild_id = ? AND user_id = ?",
            (guild_id, user_id),
        )
    return {"ok": True}


# --------------------------------------------------------------- invite API

def new_invite_code(conn):
    alphabet = "abcdefghjkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    while True:
        code = "".join(secrets.choice(alphabet) for _ in range(8))
        if not conn.execute("SELECT 1 FROM invites WHERE code = ?", (code,)).fetchone():
            return code


LOCAL_HOSTS = {"", "localhost", "127.0.0.1", "::1", "0.0.0.0"}


# ------------------------------------------------------------- uploads API

def store_upload(data, kind):
    """Write bytes to the upload dir under a random name; return that name.

    The browser's filename never touches the filesystem — we generate the name
    and derive the extension from the sniffed format, so a file called
    "evil.html" cannot be written or served as HTML.
    """
    name = f"{secrets.token_hex(16)}.{kind}"
    path = os.path.join(db.UPLOAD_DIR, name)
    os.makedirs(db.UPLOAD_DIR, exist_ok=True)
    with open(path, "wb") as fh:
        fh.write(data)
    return name


def discard_upload(stored_name):
    """Delete a stored file, ignoring the case where it is already gone.

    Only used for replaced avatars — message attachments and stickers are kept
    because old messages still point at them.
    """
    if not stored_name:
        return
    try:
        os.remove(os.path.join(db.UPLOAD_DIR, stored_name))
    except OSError:
        pass


def read_image_or_400(req, limit):
    data = req.raw
    if not data:
        raise HttpError(400, "No file was uploaded.")
    if len(data) > limit:
        mb = limit // (1024 * 1024)
        raise HttpError(413, f"That image is too large — the limit is {mb} MB.")
    kind = images.sniff(data)
    if not kind:
        raise HttpError(400, "That file isn't a PNG, JPEG, GIF or WebP image.")
    return data, kind


@route("POST", r"/api/channels/(\d+)/upload", raw=images.MAX_UPLOAD)
def api_upload_attachment(req, channel_id):
    """Raw image bytes in the body; filename and caption come from the query.

    Sending the file as the whole body avoids multipart parsing entirely —
    handy, since the stdlib's cgi module was removed in Python 3.13.
    """
    req.require_auth()
    channel_id = int(channel_id)
    channel_access_or_403(req, channel_id)

    data, kind = read_image_or_400(req, images.MAX_UPLOAD)
    original = (req.query.get("filename", [""])[0] or f"image.{kind}")[:120]
    caption = (req.query.get("caption", [""])[0] or "")[:MAX_MESSAGE]
    width, height = images.dimensions(data, kind)
    stored = store_upload(data, kind)

    with req.conn:
        cur = req.conn.execute(
            "INSERT INTO messages (channel_id, author_id, content, created_at)"
            " VALUES (?,?,?,?)",
            (channel_id, req.user["id"], caption.strip(), db.now()),
        )
        req.conn.execute(
            "INSERT INTO attachments (message_id, stored_name, original_name, mime,"
            " size, width, height, created_at) VALUES (?,?,?,?,?,?,?,?)",
            (cur.lastrowid, stored, original, images.FORMATS[kind], len(data),
             width, height, db.now()),
        )
    mark_read(req.conn, req.user["id"], channel_id, cur.lastrowid)
    return {"message": serialize_one(req.conn, cur.lastrowid, req.user["id"])}


@route("POST", r"/api/me/avatar", raw=images.MAX_AVATAR)
def api_set_avatar(req):
    """Upload a profile picture. Replaces whatever was there before."""
    req.require_auth()
    data, kind = read_image_or_400(req, images.MAX_AVATAR)
    stored = store_upload(data, kind)

    previous = req.conn.execute(
        "SELECT avatar FROM users WHERE id = ?", (req.user["id"],)
    ).fetchone()["avatar"]
    with req.conn:
        req.conn.execute(
            "UPDATE users SET avatar = ? WHERE id = ?", (stored, req.user["id"])
        )
    discard_upload(previous)
    row = req.conn.execute("SELECT * FROM users WHERE id = ?", (req.user["id"],)).fetchone()
    return {"user": me_payload(row)}


@route("DELETE", r"/api/me/avatar")
def api_clear_avatar(req):
    """Go back to the coloured initial."""
    req.require_auth()
    previous = req.conn.execute(
        "SELECT avatar FROM users WHERE id = ?", (req.user["id"],)
    ).fetchone()["avatar"]
    with req.conn:
        req.conn.execute("UPDATE users SET avatar = NULL WHERE id = ?", (req.user["id"],))
    discard_upload(previous)
    row = req.conn.execute("SELECT * FROM users WHERE id = ?", (req.user["id"],)).fetchone()
    return {"user": me_payload(row)}


# ------------------------------------------------------------- stickers API

STICKER_NAME_RE = re.compile(r"^[A-Za-z0-9_\-]{2,24}$")
MAX_STICKERS_PER_GUILD = 50


@route("GET", r"/api/guilds/(\d+)/stickers")
def api_list_stickers(req, guild_id):
    req.require_auth()
    guild_id = int(guild_id)
    guild_member_or_403(req, guild_id)
    rows = req.conn.execute(
        "SELECT s.*, u.username FROM stickers s"
        " LEFT JOIN users u ON u.id = s.creator_id"
        " WHERE s.guild_id = ? AND s.archived = 0 ORDER BY s.name COLLATE NOCASE",
        (guild_id,),
    ).fetchall()
    return {
        "stickers": [
            {
                "id": s["id"],
                "name": s["name"],
                "url": f"/uploads/{s['stored_name']}",
                "createdBy": s["username"],
            }
            for s in rows
        ]
    }


@route("POST", r"/api/guilds/(\d+)/stickers", raw=images.MAX_STICKER)
def api_create_sticker(req, guild_id):
    req.require_auth()
    guild_id = int(guild_id)
    owner_only(req, guild_id)

    name = (req.query.get("name", [""])[0] or "").strip()
    if not STICKER_NAME_RE.match(name):
        raise HttpError(400, "Sticker names are 2–24 characters: letters, numbers, - and _")

    count = req.conn.execute(
        "SELECT COUNT(*) c FROM stickers WHERE guild_id = ? AND archived = 0", (guild_id,)
    ).fetchone()["c"]
    if count >= MAX_STICKERS_PER_GUILD:
        raise HttpError(400, f"This server already has {MAX_STICKERS_PER_GUILD} stickers.")

    clash = req.conn.execute(
        "SELECT 1 FROM stickers WHERE guild_id = ? AND name = ? AND archived = 0",
        (guild_id, name),
    ).fetchone()
    if clash:
        raise HttpError(409, f"This server already has a sticker called {name}.")

    data, kind = read_image_or_400(req, images.MAX_STICKER)
    stored = store_upload(data, kind)
    with req.conn:
        cur = req.conn.execute(
            "INSERT INTO stickers (guild_id, name, stored_name, mime, creator_id,"
            " created_at) VALUES (?,?,?,?,?,?)",
            (guild_id, name, stored, images.FORMATS[kind], req.user["id"], db.now()),
        )
    return {"sticker": {"id": cur.lastrowid, "name": name, "url": f"/uploads/{stored}"}}


@route("DELETE", r"/api/stickers/(\d+)")
def api_delete_sticker(req, sticker_id):
    req.require_auth()
    s = req.conn.execute(
        "SELECT * FROM stickers WHERE id = ?", (int(sticker_id),)
    ).fetchone()
    if not s:
        raise HttpError(404, "Sticker not found.")
    owner_only(req, s["guild_id"])
    # Soft delete: messages that already used this sticker keep rendering it,
    # and the name becomes free again for a new sticker.
    with req.conn:
        req.conn.execute("UPDATE stickers SET archived = 1 WHERE id = ?", (s["id"],))
    return {"ok": True}


@route("GET", r"/api/server-info")
def api_server_info(req):
    """Where other people should point their browser to reach this server."""
    origin = req.public_origin() or ""
    host = origin.split("://")[-1].split(":")[0].strip("[]")
    if host not in LOCAL_HOSTS:
        # Deployed behind a real domain — that is exactly what to share.
        return {"lanUrl": origin, "isShareable": True}
    # Running on someone's laptop: hand out the LAN address instead of localhost.
    ip = lan_ip()
    return {
        "lanUrl": f"http://{ip}:{SERVER_PORT}",
        "isShareable": not ip.startswith("127."),
    }


@route("GET", r"/api/health")
def api_health(req):
    """Cheap liveness probe for the hosting platform."""
    return {"ok": True}


@route("POST", r"/api/guilds/(\d+)/invites")
def api_create_invite(req, guild_id):
    req.require_auth()
    guild_id = int(guild_id)
    guild_member_or_403(req, guild_id)

    max_uses = req.body.get("maxUses")
    max_uses = int(max_uses) if max_uses not in (None, "", 0, "0") else None
    expires_in = req.body.get("expiresIn")  # seconds, or None for never
    expires_at = db.now() + int(expires_in) if expires_in else None

    code = new_invite_code(req.conn)
    with req.conn:
        req.conn.execute(
            "INSERT INTO invites (code, guild_id, creator_id, max_uses, expires_at,"
            " created_at) VALUES (?,?,?,?,?,?)",
            (code, guild_id, req.user["id"], max_uses, expires_at, db.now()),
        )
    return {"code": code}


@route("GET", r"/api/guilds/(\d+)/invites")
def api_list_invites(req, guild_id):
    req.require_auth()
    guild_id = int(guild_id)
    guild_member_or_403(req, guild_id)
    rows = req.conn.execute(
        "SELECT i.*, u.username FROM invites i JOIN users u ON u.id = i.creator_id"
        " WHERE i.guild_id = ? ORDER BY i.created_at DESC",
        (guild_id,),
    ).fetchall()
    return {
        "invites": [
            {
                "code": r["code"],
                "uses": r["uses"],
                "maxUses": r["max_uses"],
                "expiresAt": r["expires_at"],
                "createdBy": r["username"],
                "expired": bool(r["expires_at"] and r["expires_at"] < db.now()),
            }
            for r in rows
        ]
    }


@route("DELETE", r"/api/invites/([A-Za-z0-9]+)")
def api_revoke_invite(req, code):
    req.require_auth()
    inv = req.conn.execute("SELECT * FROM invites WHERE code = ?", (code,)).fetchone()
    if not inv:
        raise HttpError(404, "That invite doesn't exist.")
    owner_only(req, inv["guild_id"])
    with req.conn:
        req.conn.execute("DELETE FROM invites WHERE code = ?", (code,))
    return {"ok": True}


@route("GET", r"/api/invites/([A-Za-z0-9]+)")
def api_preview_invite(req, code):
    req.require_auth()
    inv, guild = resolve_invite(req.conn, code)
    joined = req.conn.execute(
        "SELECT 1 FROM guild_members WHERE guild_id = ? AND user_id = ?",
        (guild["id"], req.user["id"]),
    ).fetchone()
    return {
        "guild": {
            "id": guild["id"],
            "name": guild["name"],
            "color": guild["color"],
            "memberCount": req.conn.execute(
                "SELECT COUNT(*) c FROM guild_members WHERE guild_id = ?", (guild["id"],)
            ).fetchone()["c"],
        },
        "alreadyMember": bool(joined),
    }


def resolve_invite(conn, code):
    inv = conn.execute("SELECT * FROM invites WHERE code = ?", (code,)).fetchone()
    if not inv:
        raise HttpError(404, "That invite code isn't valid.")
    if inv["expires_at"] and inv["expires_at"] < db.now():
        raise HttpError(410, "That invite has expired.")
    if inv["max_uses"] and inv["uses"] >= inv["max_uses"]:
        raise HttpError(410, "That invite has run out of uses.")
    guild = conn.execute("SELECT * FROM guilds WHERE id = ?", (inv["guild_id"],)).fetchone()
    if not guild:
        raise HttpError(404, "That server no longer exists.")
    return inv, guild


@route("POST", r"/api/invites/([A-Za-z0-9]+)/join")
def api_join_invite(req, code):
    req.require_auth()
    inv, guild = resolve_invite(req.conn, code)
    already = req.conn.execute(
        "SELECT 1 FROM guild_members WHERE guild_id = ? AND user_id = ?",
        (guild["id"], req.user["id"]),
    ).fetchone()
    if not already:
        with req.conn:
            req.conn.execute(
                "INSERT INTO guild_members (guild_id, user_id, joined_at) VALUES (?,?,?)",
                (guild["id"], req.user["id"], db.now()),
            )
            req.conn.execute(
                "UPDATE invites SET uses = uses + 1 WHERE code = ?", (code,)
            )
    first = req.conn.execute(
        "SELECT id FROM channels WHERE guild_id = ? AND kind = 'text' ORDER BY id LIMIT 1",
        (guild["id"],),
    ).fetchone()
    return {
        "guildId": guild["id"],
        "channelId": first["id"] if first else None,
        "alreadyMember": bool(already),
    }


# -------------------------------------------------------------- message API

@route("GET", r"/api/channels/(\d+)/messages")
def api_messages(req, channel_id):
    req.require_auth()
    channel_id = int(channel_id)
    channel_access_or_403(req, channel_id)
    after = int(req.query.get("after", ["0"])[0] or 0)
    before = int(req.query.get("before", ["0"])[0] or 0)
    limit = min(int(req.query.get("limit", ["50"])[0] or 50), 100)

    if after:
        rows = req.conn.execute(
            MESSAGE_SELECT + " WHERE m.channel_id = ? AND m.id > ? ORDER BY m.id LIMIT ?",
            (channel_id, after, limit),
        ).fetchall()
    elif before:
        rows = req.conn.execute(
            MESSAGE_SELECT + " WHERE m.channel_id = ? AND m.id < ? ORDER BY m.id DESC LIMIT ?",
            (channel_id, before, limit),
        ).fetchall()[::-1]
    else:
        rows = req.conn.execute(
            MESSAGE_SELECT + " WHERE m.channel_id = ? ORDER BY m.id DESC LIMIT ?",
            (channel_id, limit),
        ).fetchall()[::-1]

    messages = serialize_messages(req.conn, rows, req.user["id"])
    if messages:
        mark_read(req.conn, req.user["id"], channel_id, messages[-1]["id"])
    has_more = False
    if rows and not after:
        has_more = bool(
            req.conn.execute(
                "SELECT 1 FROM messages WHERE channel_id = ? AND id < ? LIMIT 1",
                (channel_id, rows[0]["id"]),
            ).fetchone()
        )
    return {"messages": messages, "hasMore": has_more}


def mark_read(conn, user_id, channel_id, message_id):
    with conn:
        conn.execute(
            "INSERT INTO read_state (user_id, channel_id, last_read_msg) VALUES (?,?,?)"
            " ON CONFLICT(user_id, channel_id) DO UPDATE SET last_read_msg = MAX(last_read_msg, ?)",
            (user_id, channel_id, message_id, message_id),
        )


@route("POST", r"/api/channels/(\d+)/messages")
def api_send_message(req, channel_id):
    req.require_auth()
    channel_id = int(channel_id)
    ch = channel_access_or_403(req, channel_id)

    sticker_id = req.body.get("stickerId")
    if sticker_id:
        sticker = req.conn.execute(
            "SELECT * FROM stickers WHERE id = ?", (int(sticker_id),)
        ).fetchone()
        if not sticker:
            raise HttpError(404, "That sticker no longer exists.")
        # Stickers belong to a server; you must be in it to use them, and in a
        # DM you may use stickers from any server you are a member of.
        guild_member_or_403(req, sticker["guild_id"])
        if ch["kind"] == "text" and ch["guild_id"] != sticker["guild_id"]:
            raise HttpError(403, "That sticker belongs to a different server.")
        content = ""
    else:
        sticker_id = None
        content = req.field("content", maxlen=MAX_MESSAGE)

    with req.conn:
        cur = req.conn.execute(
            "INSERT INTO messages (channel_id, author_id, content, created_at, sticker_id)"
            " VALUES (?,?,?,?,?)",
            (channel_id, req.user["id"], content, db.now(), sticker_id),
        )
    mark_read(req.conn, req.user["id"], channel_id, cur.lastrowid)
    return {"message": serialize_one(req.conn, cur.lastrowid, req.user["id"])}


@route("DELETE", r"/api/messages/(\d+)")
def api_delete_message(req, message_id):
    req.require_auth()
    msg = req.conn.execute(
        "SELECT * FROM messages WHERE id = ?", (int(message_id),)
    ).fetchone()
    if not msg:
        raise HttpError(404, "Message not found.")
    ch = channel_access_or_403(req, msg["channel_id"])
    allowed = msg["author_id"] == req.user["id"]
    if not allowed and ch["kind"] == "text":
        g = req.conn.execute(
            "SELECT owner_id FROM guilds WHERE id = ?", (ch["guild_id"],)
        ).fetchone()
        allowed = g and g["owner_id"] == req.user["id"]
    if not allowed:
        raise HttpError(403, "You can't delete that message.")
    with req.conn:
        req.conn.execute("DELETE FROM messages WHERE id = ?", (msg["id"],))
    return {"ok": True}


@route("PATCH", r"/api/messages/(\d+)")
def api_edit_message(req, message_id):
    req.require_auth()
    msg = req.conn.execute(
        "SELECT * FROM messages WHERE id = ?", (int(message_id),)
    ).fetchone()
    if not msg:
        raise HttpError(404, "Message not found.")
    if msg["author_id"] != req.user["id"]:
        raise HttpError(403, "You can only edit your own messages.")
    content = req.field("content", maxlen=MAX_MESSAGE)
    with req.conn:
        req.conn.execute(
            "UPDATE messages SET content = ?, edited_at = ? WHERE id = ?",
            (content, db.now(), msg["id"]),
        )
    return {"message": serialize_one(req.conn, msg["id"], req.user["id"])}


# ------------------------------------------------------------- reactions API

# Reactions are stored as literal emoji characters. Cap the length so nobody
# can stuff a paragraph into the field; a few codepoints covers flags and
# skin-tone/ZWJ sequences.
MAX_EMOJI_LEN = 24


@route("POST", r"/api/messages/(\d+)/reactions")
def api_add_reaction(req, message_id):
    """Toggle: reacting with an emoji you already used removes it."""
    req.require_auth()
    msg = req.conn.execute(
        "SELECT * FROM messages WHERE id = ?", (int(message_id),)
    ).fetchone()
    if not msg:
        raise HttpError(404, "Message not found.")
    channel_access_or_403(req, msg["channel_id"])

    emoji = req.field("emoji", maxlen=MAX_EMOJI_LEN)
    if any(c.isspace() for c in emoji):
        raise HttpError(400, "That isn't a valid emoji.")

    existing = req.conn.execute(
        "SELECT id FROM reactions WHERE message_id = ? AND user_id = ? AND emoji = ?",
        (msg["id"], req.user["id"], emoji),
    ).fetchone()
    with req.conn:
        if existing:
            req.conn.execute("DELETE FROM reactions WHERE id = ?", (existing["id"],))
        else:
            req.conn.execute(
                "INSERT INTO reactions (message_id, user_id, emoji, created_at)"
                " VALUES (?,?,?,?)",
                (msg["id"], req.user["id"], emoji, db.now()),
            )
    return {"message": serialize_one(req.conn, msg["id"], req.user["id"])}


@route("GET", r"/api/messages/(\d+)/reactions")
def api_list_reactors(req, message_id):
    """Who reacted with what — powers the tooltip on a reaction pill."""
    req.require_auth()
    msg = req.conn.execute(
        "SELECT * FROM messages WHERE id = ?", (int(message_id),)
    ).fetchone()
    if not msg:
        raise HttpError(404, "Message not found.")
    channel_access_or_403(req, msg["channel_id"])
    rows = req.conn.execute(
        "SELECT r.emoji, u.username FROM reactions r JOIN users u ON u.id = r.user_id"
        " WHERE r.message_id = ? ORDER BY r.id",
        (msg["id"],),
    ).fetchall()
    grouped = {}
    for r in rows:
        grouped.setdefault(r["emoji"], []).append(r["username"])
    return {"reactors": grouped}


@route("POST", r"/api/channels/(\d+)/read")
def api_mark_read(req, channel_id):
    req.require_auth()
    channel_id = int(channel_id)
    channel_access_or_403(req, channel_id)
    top = req.conn.execute(
        "SELECT COALESCE(MAX(id), 0) v FROM messages WHERE channel_id = ?", (channel_id,)
    ).fetchone()["v"]
    mark_read(req.conn, req.user["id"], channel_id, top)
    return {"ok": True}


# --------------------------------------------------------------- friend API

@route("GET", r"/api/friends")
def api_friends(req):
    req.require_auth()
    uid = req.user["id"]
    rows = req.conn.execute(
        "SELECT f.*, "
        "  ru.username ru_name, ru.discriminator ru_disc, ru.color ru_color,"
        "  ru.last_seen ru_seen, ru.bio ru_bio,"
        "  au.username au_name, au.discriminator au_disc, au.color au_color,"
        "  au.last_seen au_seen, au.bio au_bio"
        " FROM friendships f"
        " JOIN users ru ON ru.id = f.requester_id"
        " JOIN users au ON au.id = f.addressee_id"
        " WHERE f.requester_id = ? OR f.addressee_id = ?",
        (uid, uid),
    ).fetchall()

    friends, incoming, outgoing = [], [], []
    for r in rows:
        other_is_requester = r["addressee_id"] == uid
        p = "ru_" if other_is_requester else "au_"
        other = {
            "id": r["requester_id"] if other_is_requester else r["addressee_id"],
            "username": r[p + "name"],
            "discriminator": r[p + "disc"],
            "tag": f'{r[p + "name"]}#{r[p + "disc"]}',
            "color": r[p + "color"],
            "bio": r[p + "bio"],
            "online": (db.now() - (r[p + "seen"] or 0)) < db.ONLINE_WINDOW,
            "friendshipId": r["id"],
        }
        if r["status"] == "accepted":
            friends.append(other)
        elif other_is_requester:
            incoming.append(other)
        else:
            outgoing.append(other)

    friends.sort(key=lambda f: (not f["online"], f["username"].lower()))
    return {"friends": friends, "incoming": incoming, "outgoing": outgoing}


@route("POST", r"/api/friends")
def api_add_friend(req):
    req.require_auth()
    raw = req.field("tag", maxlen=64)
    if "#" not in raw:
        raise HttpError(400, "Use the full tag, e.g. Alex#0421")
    name, _, disc = raw.rpartition("#")
    target = req.conn.execute(
        "SELECT * FROM users WHERE username = ? AND discriminator = ?",
        (name.strip(), disc.strip()),
    ).fetchone()
    if not target:
        raise HttpError(404, "No one with that tag. Check the spelling and the four digits.")
    if target["id"] == req.user["id"]:
        raise HttpError(400, "You can't add yourself.")

    existing = req.conn.execute(
        "SELECT * FROM friendships WHERE (requester_id = ? AND addressee_id = ?)"
        " OR (requester_id = ? AND addressee_id = ?)",
        (req.user["id"], target["id"], target["id"], req.user["id"]),
    ).fetchone()
    if existing:
        if existing["status"] == "accepted":
            raise HttpError(409, f'You and {target["username"]} are already friends.')
        if existing["requester_id"] == target["id"]:
            # They asked first — accept instead of duplicating.
            with req.conn:
                req.conn.execute(
                    "UPDATE friendships SET status = 'accepted' WHERE id = ?",
                    (existing["id"],),
                )
            return {"status": "accepted"}
        raise HttpError(409, "You already have a request pending with them.")

    with req.conn:
        req.conn.execute(
            "INSERT INTO friendships (requester_id, addressee_id, status, created_at)"
            " VALUES (?,?,'pending',?)",
            (req.user["id"], target["id"], db.now()),
        )
    return {"status": "pending"}


def owned_friendship(req, friendship_id):
    f = req.conn.execute(
        "SELECT * FROM friendships WHERE id = ?", (int(friendship_id),)
    ).fetchone()
    if not f or req.user["id"] not in (f["requester_id"], f["addressee_id"]):
        raise HttpError(404, "Request not found.")
    return f


@route("POST", r"/api/friends/(\d+)/accept")
def api_accept_friend(req, friendship_id):
    req.require_auth()
    f = owned_friendship(req, friendship_id)
    if f["addressee_id"] != req.user["id"]:
        raise HttpError(403, "Only the person who received the request can accept it.")
    with req.conn:
        req.conn.execute(
            "UPDATE friendships SET status = 'accepted' WHERE id = ?", (f["id"],)
        )
    return {"ok": True}


@route("DELETE", r"/api/friends/(\d+)")
def api_remove_friend(req, friendship_id):
    req.require_auth()
    f = owned_friendship(req, friendship_id)
    with req.conn:
        req.conn.execute("DELETE FROM friendships WHERE id = ?", (f["id"],))
    return {"ok": True}


# ------------------------------------------------------------------- DM API

@route("GET", r"/api/dms")
def api_dms(req):
    req.require_auth()
    uid = req.user["id"]
    rows = req.conn.execute(
        "SELECT c.id FROM channels c JOIN dm_participants p ON p.channel_id = c.id"
        " WHERE c.kind = 'dm' AND p.user_id = ?",
        (uid,),
    ).fetchall()
    dms = []
    for r in rows:
        other = dm_partner(req.conn, r["id"], uid)
        if not other:
            continue
        last = req.conn.execute(
            "SELECT id, content, created_at FROM messages WHERE channel_id = ?"
            " ORDER BY id DESC LIMIT 1",
            (r["id"],),
        ).fetchone()
        dms.append(
            {
                "channelId": r["id"],
                "user": db.public_user(other, db.is_online(other)),
                "unread": unread_count(req.conn, uid, r["id"]),
                "lastMessage": last["content"] if last else None,
                "lastAt": last["created_at"] if last else 0,
            }
        )
    dms.sort(key=lambda d: d["lastAt"], reverse=True)
    return {"dms": dms}


@route("POST", r"/api/dms")
def api_open_dm(req):
    req.require_auth()
    other_id = int(req.field("userId"))
    uid = req.user["id"]
    if other_id == uid:
        raise HttpError(400, "You can't DM yourself.")
    other = req.conn.execute("SELECT * FROM users WHERE id = ?", (other_id,)).fetchone()
    if not other:
        raise HttpError(404, "User not found.")

    existing = req.conn.execute(
        "SELECT p1.channel_id id FROM dm_participants p1"
        " JOIN dm_participants p2 ON p1.channel_id = p2.channel_id"
        " WHERE p1.user_id = ? AND p2.user_id = ?",
        (uid, other_id),
    ).fetchone()
    if existing:
        return {"channelId": existing["id"]}

    with req.conn:
        cur = req.conn.execute(
            "INSERT INTO channels (guild_id, kind, name, created_at) VALUES (NULL,'dm','',?)",
            (db.now(),),
        )
        cid = cur.lastrowid
        req.conn.executemany(
            "INSERT INTO dm_participants (channel_id, user_id) VALUES (?,?)",
            [(cid, uid), (cid, other_id)],
        )
    return {"channelId": cid}


@route("GET", r"/api/channels/(\d+)")
def api_channel_info(req, channel_id):
    req.require_auth()
    channel_id = int(channel_id)
    ch = channel_access_or_403(req, channel_id)
    if ch["kind"] == "dm":
        other = dm_partner(req.conn, channel_id, req.user["id"])
        return {
            "channel": {
                "id": ch["id"],
                "kind": "dm",
                "name": other["username"] if other else "Unknown",
                "user": db.public_user(other, db.is_online(other)) if other else None,
            }
        }
    return {
        "channel": {
            "id": ch["id"],
            "kind": "text",
            "name": ch["name"],
            "topic": ch["topic"],
            "guildId": ch["guild_id"],
        }
    }


# ------------------------------------------------------------------ polling

@route("GET", r"/api/poll")
def api_poll(req):
    req.require_auth()
    uid = req.user["id"]
    channel_id = req.query.get("channel", [None])[0]
    channel_id = int(channel_id) if channel_id and channel_id.isdigit() else None
    after = int(req.query.get("after", ["0"])[0] or 0)
    client_rev = req.query.get("rev", [""])[0]

    if channel_id:
        channel_access_or_403(req, channel_id)

    with req.conn:
        req.conn.execute("UPDATE users SET last_seen = ? WHERE id = ?", (db.now(), uid))

    client_chan_rev = req.query.get("crev", [""])[0]

    deadline = time.monotonic() + POLL_TIMEOUT
    while True:
        rev = revision(req.conn, uid)
        chan_rev = channel_revision(req.conn, channel_id) if channel_id else ""
        messages = []
        if channel_id:
            rows = req.conn.execute(
                MESSAGE_SELECT + " WHERE m.channel_id = ? AND m.id > ? ORDER BY m.id LIMIT 100",
                (channel_id, after),
            ).fetchall()
            messages = serialize_messages(req.conn, rows, uid)
        chan_changed = chan_rev != client_chan_rev
        if messages or rev != client_rev or chan_changed or time.monotonic() >= deadline:
            if messages:
                mark_read(req.conn, uid, channel_id, messages[-1]["id"])
                rev = revision(req.conn, uid)
                chan_rev = channel_revision(req.conn, channel_id)
            return {
                "messages": messages,
                "rev": rev,
                "changed": rev != client_rev,
                "channelRev": chan_rev,
                # Reactions, edits and deletions don't create new messages, so
                # the client refetches the visible window when this flips.
                "channelChanged": chan_changed,
            }
        time.sleep(POLL_INTERVAL)
        # The client aborts this request on every channel switch. Without
        # this check the thread would keep querying for the full timeout.
        if req.client_gone():
            raise ClientGone()


@route("GET", r"/api/users/(\d+)")
def api_user_profile(req, user_id):
    req.require_auth()
    row = req.conn.execute("SELECT * FROM users WHERE id = ?", (int(user_id),)).fetchone()
    if not row:
        raise HttpError(404, "User not found.")
    payload = db.public_user(row, db.is_online(row))
    payload["createdAt"] = row["created_at"]
    f = req.conn.execute(
        "SELECT * FROM friendships WHERE (requester_id = ? AND addressee_id = ?)"
        " OR (requester_id = ? AND addressee_id = ?)",
        (req.user["id"], row["id"], row["id"], req.user["id"]),
    ).fetchone()
    payload["friendship"] = (
        {"id": f["id"], "status": f["status"], "incoming": f["addressee_id"] == req.user["id"]}
        if f
        else None
    )
    return {"user": payload}


# --------------------------------------------------------------- HTTP layer

class Handler(BaseHTTPRequestHandler):
    server_version = "Nexus"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        if "--verbose" in os.environ.get("NEXUS_FLAGS", ""):
            super().log_message(fmt, *args)

    # -- plumbing ---------------------------------------------------------

    def _cookies(self):
        """Parse the Cookie header by hand.

        http.cookies.SimpleCookie silently throws away every morsel after the
        first one it cannot parse. Cookies are shared across all ports on a
        host, so any other dev server the user has ever run on localhost sends
        its cookies here too — one of those with a space or a JSON value would
        wipe out our session cookie and log the user straight back out.
        """
        jar = {}
        raw = "; ".join(self.headers.get_all("Cookie") or [])
        for part in raw.split(";"):
            name, sep, value = part.partition("=")
            if not sep:
                continue
            name, value = name.strip(), value.strip()
            if len(value) >= 2 and value[0] == '"' and value[-1] == '"':
                value = value[1:-1]
            if name:
                jar[name] = value
        return jar

    # Absurdly large uploads aren't worth reading just to be polite about it.
    DRAIN_CAP = 64 * 1024 * 1024

    def _read_raw(self, limit):
        """Read the body as bytes — used for image uploads."""
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return b""
        if length > limit:
            # Swallow what the client is still sending, otherwise it hits a
            # broken pipe mid-upload and never gets to read our 413.
            if length <= self.DRAIN_CAP:
                remaining = length
                while remaining > 0:
                    chunk = self.rfile.read(min(65536, remaining))
                    if not chunk:
                        break
                    remaining -= len(chunk)
            else:
                self.close_connection = True
            mb = limit // (1024 * 1024)
            raise HttpError(413, f"That file is too large — the limit is {mb} MB.")
        return self.rfile.read(length)

    def _read_body(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        if length > MAX_BODY:
            raise HttpError(413, "That request is too large.")
        raw = self.rfile.read(length)
        try:
            parsed = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise HttpError(400, "Malformed JSON body.")
        if not isinstance(parsed, dict):
            raise HttpError(400, "Request body must be a JSON object.")
        return parsed

    def _send_json(self, status, payload, req=None):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        # Browsers silently drop a Secure cookie sent over plain http, which
        # would log everyone out locally — so only add it on real TLS requests.
        proto = (self.headers.get("X-Forwarded-Proto") or "").split(",")[0].strip().lower()
        secure = "; Secure" if proto == "https" else ""
        if req and req.set_cookie:
            self.send_header(
                "Set-Cookie",
                f"session={req.set_cookie}; Path=/; HttpOnly; SameSite=Lax"
                f"; Max-Age=2592000{secure}",
            )
        if req and req.clear_cookie:
            self.send_header(
                "Set-Cookie",
                f"session=; Path=/; HttpOnly; SameSite=Lax; Max-Age=0{secure}",
            )
        self.end_headers()
        self.wfile.write(body)

    # Only names this server generated: 32 hex characters plus a known suffix.
    UPLOAD_NAME_RE = re.compile(r"^[0-9a-f]{32}\.(png|jpg|gif|webp)$")

    def _serve_upload(self, name):
        """Serve a stored image.

        The strict name pattern makes path traversal impossible, and the
        response is pinned to the image type recorded at upload time with
        nosniff, so a file can never be interpreted as HTML or script.
        """
        match = self.UPLOAD_NAME_RE.match(unquote(name))
        if not match:
            self._send_json(404, {"error": "Not found."})
            return
        full = os.path.join(db.UPLOAD_DIR, match.group(0))
        if not os.path.isfile(full):
            self._send_json(404, {"error": "Not found."})
            return
        with open(full, "rb") as fh:
            data = fh.read()
        self.send_response(200)
        self.send_header("Content-Type", images.FORMATS[match.group(1)])
        self.send_header("Content-Length", str(len(data)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Disposition", "inline")
        self.send_header("Content-Security-Policy", "default-src 'none'; sandbox")
        # Names are random and content never changes, so cache hard.
        self.send_header("Cache-Control", "public, max-age=31536000, immutable")
        self.end_headers()
        self.wfile.write(data)

    def _serve_static(self, path):
        if path == "/" or path.startswith("/invite/") or path == "/app":
            path = "/index.html"
        rel = unquote(path.lstrip("/"))
        full = os.path.normpath(os.path.join(STATIC_DIR, rel))
        if not full.startswith(STATIC_DIR) or not os.path.isfile(full):
            self._send_json(404, {"error": "Not found."})
            return
        ctype, _ = mimetypes.guess_type(full)
        with open(full, "rb") as fh:
            data = fh.read()
        self.send_response(200)
        self.send_header("Content-Type", ctype or "application/octet-stream")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(data)

    # -- dispatch ---------------------------------------------------------

    def _handle(self, method):
        parsed = urlparse(self.path)
        path = parsed.path

        if path.startswith("/uploads/"):
            if method != "GET":
                self._send_json(405, {"error": "Method not allowed."})
            else:
                self._serve_upload(path[len("/uploads/"):])
            return

        if not path.startswith("/api/"):
            if method != "GET":
                self._send_json(405, {"error": "Method not allowed."})
            else:
                self._serve_static(path)
            return

        conn = db.connect()
        req = None
        try:
            # Find the route first: it decides whether the body is JSON or a
            # raw file. The body must be drained either way to keep the
            # keep-alive connection in sync.
            handler_fn = groups = None
            raw_limit = 0
            for m, pattern, fn, limit in ROUTES:
                if m != method:
                    continue
                match = pattern.match(path)
                if match:
                    handler_fn, groups, raw_limit = fn, match.groups(), limit
                    break

            body, raw = {}, b""
            if raw_limit:
                raw = self._read_raw(raw_limit)
            elif method in ("POST", "PATCH", "PUT"):
                body = self._read_body()

            cookies = self._cookies()
            user = None
            token = cookies.get("session")
            refresh_cookie = False
            if token:
                row = conn.execute(
                    "SELECT u.*, s.last_used FROM sessions s JOIN users u ON u.id = s.user_id"
                    " WHERE s.token = ?",
                    (token,),
                ).fetchone()
                if row and (db.now() - row["last_used"]) < db.SESSION_IDLE_TTL:
                    user = dict(row)
                    # Slide the expiry forward for anyone who is still active.
                    if db.now() - row["last_used"] > db.SESSION_TOUCH_EVERY:
                        with conn:
                            conn.execute(
                                "UPDATE sessions SET last_used = ? WHERE token = ?",
                                (db.now(), token),
                            )
                        refresh_cookie = True
            req = Req(conn, body, parse_qs(parsed.query), cookies, user, self, raw)
            if refresh_cookie:
                req.set_cookie = token

            if handler_fn:
                self._send_json(200, handler_fn(req, *groups), req)
            else:
                self._send_json(404, {"error": "Unknown endpoint."})
        except ClientGone:
            self.close_connection = True
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True
        except HttpError as exc:
            self._send_json(exc.status, {"error": exc.message}, req)
        except Exception as exc:  # pragma: no cover - surface bugs to the client
            import traceback

            traceback.print_exc()
            try:
                self._send_json(500, {"error": f"Server error: {exc}"})
            except OSError:
                self.close_connection = True
        finally:
            conn.close()

    def do_GET(self):
        self._handle("GET")

    def do_POST(self):
        self._handle("POST")

    def do_PATCH(self):
        self._handle("PATCH")

    def do_DELETE(self):
        self._handle("DELETE")


class Server(ThreadingHTTPServer):
    daemon_threads = True
    # Keep the listen backlog generous: every open tab holds a long poll.
    request_queue_size = 64

    def handle_error(self, request, client_address):
        """The client hanging up is routine here, not a fault worth logging.

        Every channel switch aborts an in-flight long poll, which surfaces as a
        connection reset. Logging a traceback for each one would bury real
        errors in the deploy logs.
        """
        exc = sys.exc_info()[1]
        if isinstance(exc, (ConnectionResetError, BrokenPipeError, TimeoutError)):
            return
        super().handle_error(request, client_address)


def main():
    parser = argparse.ArgumentParser(description="Run the Nexus messaging server.")
    # Hosts like Railway, Render and Fly assign the port through $PORT.
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT") or 8000))
    parser.add_argument("--host", default=os.environ.get("HOST") or "0.0.0.0",
                        help="Interface to bind. Defaults to 0.0.0.0 so other people"
                             " on your network (or your host platform) can reach it;"
                             " pass 127.0.0.1 to keep it to this machine only.")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()
    if args.verbose:
        os.environ["NEXUS_FLAGS"] = "--verbose"

    global SERVER_PORT
    SERVER_PORT = args.port

    db.init()
    server = Server((args.host, args.port), Handler)

    # Railway and similar set this; when present the app is already public.
    public_domain = (
        os.environ.get("RAILWAY_PUBLIC_DOMAIN")
        or os.environ.get("RENDER_EXTERNAL_HOSTNAME")
        or os.environ.get("PUBLIC_DOMAIN")
    )

    print("Nexus is running.\n", flush=True)
    if public_domain:
        print(f"  Public address:    https://{public_domain}")
        print(f"  Listening on:      {args.host}:{args.port}")
        print("\n  Anyone can open that address, create an account,")
        print("  and join a server with an invite code.")
    else:
        print(f"  On this computer:  http://localhost:{args.port}")
        if args.host == "0.0.0.0":
            print(f"  Share with others: http://{lan_ip()}:{args.port}")
            print("\n  Anyone on the same Wi-Fi can open that second address,")
            print("  create an account, and join with an invite code.")
        else:
            print("\n  Bound to this machine only — others cannot connect.")
            print("  Restart without --host 127.0.0.1 to let people in.")
    print(f"\nDatabase: {db.DB_PATH}", flush=True)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down.")
        server.server_close()


if __name__ == "__main__":
    main()
