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


def route(method, pattern):
    compiled = re.compile("^" + pattern + "$")

    def deco(fn):
        ROUTES.append((method, compiled, fn))
        return fn

    return deco


class Req:
    """Everything a handler needs: db connection, body, query, current user."""

    def __init__(self, conn, body, query, cookies, user, handler=None):
        self.conn = conn
        self.body = body
        self.query = query
        self.cookies = cookies
        self.user = user
        self.handler = handler
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


def serialize_message(row):
    return {
        "id": row["id"],
        "channelId": row["channel_id"],
        "content": row["content"],
        "createdAt": row["created_at"],
        "editedAt": row["edited_at"],
        "author": {
            "id": row["author_id"],
            "username": row["username"],
            "discriminator": row["discriminator"],
            "tag": f'{row["username"]}#{row["discriminator"]}',
            "color": row["color"],
        },
    }


MESSAGE_SELECT = """
SELECT m.*, u.username, u.discriminator, u.color
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

    messages = [serialize_message(r) for r in rows]
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
    channel_access_or_403(req, channel_id)
    content = req.field("content", maxlen=MAX_MESSAGE)
    with req.conn:
        cur = req.conn.execute(
            "INSERT INTO messages (channel_id, author_id, content, created_at)"
            " VALUES (?,?,?,?)",
            (channel_id, req.user["id"], content, db.now()),
        )
    mark_read(req.conn, req.user["id"], channel_id, cur.lastrowid)
    row = req.conn.execute(
        MESSAGE_SELECT + " WHERE m.id = ?", (cur.lastrowid,)
    ).fetchone()
    return {"message": serialize_message(row)}


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
    row = req.conn.execute(MESSAGE_SELECT + " WHERE m.id = ?", (msg["id"],)).fetchone()
    return {"message": serialize_message(row)}


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

    deadline = time.monotonic() + POLL_TIMEOUT
    while True:
        rev = revision(req.conn, uid)
        messages = []
        if channel_id:
            rows = req.conn.execute(
                MESSAGE_SELECT + " WHERE m.channel_id = ? AND m.id > ? ORDER BY m.id LIMIT 100",
                (channel_id, after),
            ).fetchall()
            messages = [serialize_message(r) for r in rows]
        if messages or rev != client_rev or time.monotonic() >= deadline:
            if messages:
                mark_read(req.conn, uid, channel_id, messages[-1]["id"])
                rev = revision(req.conn, uid)
            return {"messages": messages, "rev": rev, "changed": rev != client_rev}
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

        if not path.startswith("/api/"):
            if method != "GET":
                self._send_json(405, {"error": "Method not allowed."})
            else:
                self._serve_static(path)
            return

        conn = db.connect()
        req = None
        try:
            body = self._read_body() if method in ("POST", "PATCH", "PUT") else {}
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
            req = Req(conn, body, parse_qs(parsed.query), cookies, user, self)
            if refresh_cookie:
                req.set_cookie = token

            for m, pattern, fn in ROUTES:
                if m != method:
                    continue
                match = pattern.match(path)
                if match:
                    result = fn(req, *match.groups())
                    self._send_json(200, result, req)
                    return
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
