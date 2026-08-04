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
import ssl
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import urllib.error
import urllib.request
from urllib.parse import urlparse, parse_qs, unquote, urlencode

import blackjack
import db
import images
import poker
import roulette
import slots

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

def public_user(conn, row, online=None):
    """db.public_user, plus the shop perks that change how a name is drawn.

    Perks need a query, which db.public_user has no connection to make, so
    every caller that has one goes through here instead.
    """
    if online is None:
        online = db.is_online(row)
    return db.public_user(row, online, db.perks_for(conn, row["id"]))


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
        # A locked channel is one the shop sells the key to. Owning the server
        # is not enough — the whole point is that the key has to be bought.
        if locked_of(ch) and locked_of(ch) not in db.perks_for(req.conn, req.user["id"]):
            raise HttpError(403, "That channel is locked. The Frontman sells the key.")
    return ch


def locked_of(channel):
    """Which perk unlocks this channel, or '' for an ordinary one."""
    keys = channel.keys()
    return channel["locked"] if "locked" in keys and channel["locked"] else ""


# A mention is written as the author's full tag so the composer stays readable;
# the client renders it as a plain @name. Usernames may contain spaces and dots,
# and the four digits make it unambiguous.
MENTION_RE = re.compile(r"@([A-Za-z0-9_.\- ]{2,32})#(\d{4})")
EVERYONE_RE = re.compile(r"@everyone\b")


def mention_targets(conn, channel, content, author_id):
    """User ids pinged by this message, excluding the author."""
    targets = set()
    for name, disc in MENTION_RE.findall(content or ""):
        row = conn.execute(
            "SELECT id FROM users WHERE username = ? AND discriminator = ?",
            (name.strip(), disc),
        ).fetchone()
        if row:
            targets.add(row["id"])

    if EVERYONE_RE.search(content or ""):
        if channel["kind"] == "dm":
            rows = conn.execute(
                "SELECT user_id id FROM dm_participants WHERE channel_id = ?",
                (channel["id"],),
            )
        else:
            rows = conn.execute(
                "SELECT user_id id FROM guild_members WHERE guild_id = ?",
                (channel["guild_id"],),
            )
        targets.update(r["id"] for r in rows)

    targets.discard(author_id)
    # Only people who can actually see the channel get pinged.
    return {uid for uid in targets if can_read_channel(conn, channel, uid)}


def can_read_channel(conn, channel, user_id):
    if channel["kind"] == "dm":
        q = "SELECT 1 FROM dm_participants WHERE channel_id = ? AND user_id = ?"
        args = (channel["id"], user_id)
    else:
        if locked_of(channel) and locked_of(channel) not in db.perks_for(conn, user_id):
            return False
        q = "SELECT 1 FROM guild_members WHERE guild_id = ? AND user_id = ?"
        args = (channel["guild_id"], user_id)
    return bool(conn.execute(q, args).fetchone())


def record_mentions(conn, channel, message_id, content, author_id):
    with conn:
        conn.execute("DELETE FROM mentions WHERE message_id = ?", (message_id,))
        for uid in mention_targets(conn, channel, content, author_id):
            conn.execute(
                "INSERT OR IGNORE INTO mentions (message_id, user_id) VALUES (?,?)",
                (message_id, uid),
            )


def serialize_messages(conn, rows, user_id):
    """Serialize messages, batching attachments/reactions to avoid N+1 queries."""
    out = [serialize_message(r) for r in rows]
    if not out:
        return out
    by_id = {m["id"]: m for m in out}
    ids = list(by_id)
    marks = ",".join("?" * len(ids))

    # Shop perks decide how an author's name is drawn, so they travel with it.
    perks = db.perks_map(conn, {m["author"]["id"] for m in out})
    for m in out:
        m["author"]["perks"] = sorted(perks.get(m["author"]["id"], ()))

    # Which of these ping the viewer
    for r in conn.execute(
        f"SELECT message_id FROM mentions WHERE user_id = ? AND message_id IN ({marks})",
        [user_id] + ids,
    ):
        by_id[r["message_id"]]["mentionsMe"] = True

    # Replied-to previews
    reply_ids = [m["replyToId"] for m in out if m["replyToId"]]
    if reply_ids:
        rmarks = ",".join("?" * len(reply_ids))
        parents = {
            p["id"]: p
            for p in conn.execute(
                "SELECT m.id, m.content, m.sticker_id, m.author_id, u.username,"
                " u.color, u.avatar FROM messages m JOIN users u ON u.id = m.author_id"
                f" WHERE m.id IN ({rmarks})",
                reply_ids,
            )
        }
        has_image = {
            r["message_id"]
            for r in conn.execute(
                f"SELECT DISTINCT message_id FROM attachments WHERE message_id IN ({rmarks})",
                reply_ids,
            )
        }
        for m in out:
            p = parents.get(m["replyToId"])
            if not p:
                continue
            preview = p["content"]
            if not preview:
                preview = "sent a sticker" if p["sticker_id"] else (
                    "sent an image" if p["id"] in has_image else "")
            m["replyTo"] = {
                "id": p["id"],
                "content": preview,
                "author": {
                    "id": p["author_id"],
                    "username": p["username"],
                    "color": p["color"],
                    "avatarUrl": f"/uploads/{p['avatar']}" if p["avatar"] else None,
                },
            }

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

    game_ids = [m["gameId"] for m in out if m["gameId"]]
    if game_ids:
        gmarks = ",".join("?" * len(game_ids))
        games = {
            g["id"]: g
            for g in conn.execute(f"SELECT * FROM games WHERE id IN ({gmarks})", game_ids)
        }
        # Every card shows the viewer's balance, and it is the same number on
        # all of them, so it is looked up once for the whole page.
        purse = db.balance(conn, user_id)
        for m in out:
            g = games.get(m["gameId"])
            if g:
                m["game"] = serialize_game(conn, g, user_id, purse)

    poll_ids = [m["pollId"] for m in out if m["pollId"]]
    if poll_ids:
        pmarks = ",".join("?" * len(poll_ids))
        polls = {
            p["id"]: p
            for p in conn.execute(f"SELECT * FROM polls WHERE id IN ({pmarks})", poll_ids)
        }
        for m in out:
            p = polls.get(m["pollId"])
            if p:
                m["poll"] = serialize_poll(conn, p, user_id)

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
        "replyToId": row["reply_to"] if "reply_to" in keys else None,
        "replyTo": None,
        "gameId": row["game_id"] if "game_id" in keys else None,
        "game": None,
        "pollId": row["poll_id"] if "poll_id" in keys else None,
        "poll": None,
        "mentionsMe": False,
        "attachments": [],
        "reactions": [],
        "author": {
            "id": row["author_id"],
            "username": row["username"],
            "discriminator": row["discriminator"],
            "tag": f'{row["username"]}#{row["discriminator"]}',
            "color": row["color"],
            "avatarUrl": f"/uploads/{row['avatar']}" if row["avatar"] else None,
            "isBot": bool(row["is_bot"]),
            "decoration": db.decoration_of(row),
            # Filled in by serialize_messages, which can batch the lookup.
            "perks": [],
        },
    }


MESSAGE_SELECT = """
SELECT m.*, u.username, u.discriminator, u.color, u.avatar, u.is_bot,
       u.decoration
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


def mention_count(conn, user_id, channel_id):
    """Unread pings for this user in this channel."""
    return conn.execute(
        "SELECT COUNT(*) c FROM mentions mn"
        " JOIN messages m ON m.id = mn.message_id"
        " LEFT JOIN read_state r ON r.user_id = ? AND r.channel_id = m.channel_id"
        " WHERE mn.user_id = ? AND m.channel_id = ?"
        "   AND m.id > COALESCE(r.last_read_msg, 0)",
        (user_id, user_id, channel_id),
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
    # Game moves rewrite a row in place without touching the message, so the
    # board would never refresh for the other player without this.
    g = conn.execute(
        "SELECT COALESCE(MAX(id), 0) a, COALESCE(SUM(version), 0) b FROM games"
        " WHERE channel_id = ?",
        (channel_id,),
    ).fetchone()
    # Votes are rows of their own and never touch the message, so a poll would
    # sit at its opening tally for everyone but the person clicking.
    p = conn.execute(
        "SELECT COALESCE(MAX(v.id), 0) a, COUNT(v.id) b, COALESCE(SUM(p.closed), 0) c"
        " FROM polls p LEFT JOIN poll_votes v ON v.poll_id = p.id"
        " WHERE p.channel_id = ?",
        (channel_id,),
    ).fetchone()
    return (f"{m['a']}.{m['b']}.{m['c']}-{r['a']}.{r['b']}-{g['a']}.{g['b']}"
            f"-{p['a']}.{p['b']}.{p['c']}")


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
    if row["is_bot"]:
        raise HttpError(403, "That account belongs to a bot.")
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
    return {"user": me_payload(row, db.perks_for(req.conn, row["id"]))}


@route("POST", r"/api/logout")
def api_logout(req):
    token = req.cookies.get("session")
    if token:
        with req.conn:
            req.conn.execute("DELETE FROM sessions WHERE token = ?", (token,))
    req.clear_cookie = True
    return {"ok": True}


def me_payload(row, perks=()):
    return {
        "id": row["id"],
        "perks": sorted(perks),
        "username": row["username"],
        "discriminator": row["discriminator"],
        "tag": f'{row["username"]}#{row["discriminator"]}',
        "email": row["email"],
        "color": row["color"],
        "bio": row["bio"],
        "status": row["status"],
        "decoration": db.decoration_of(row),
        "avatarUrl": db.avatar_url(row),
    }


@route("GET", r"/api/me")
def api_me(req):
    if not req.user:
        return {"user": None}
    row = req.conn.execute("SELECT * FROM users WHERE id = ?", (req.user["id"],)).fetchone()
    return {"user": me_payload(row, db.perks_for(req.conn, row["id"]))}


@route("PATCH", r"/api/me")
def api_update_me(req):
    req.require_auth()
    fields, values = [], []
    if "bio" in req.body:
        fields.append("bio = ?")
        values.append(str(req.body["bio"])[:190])
    if "status" in req.body:
        # One short line — anything longer would wreck the member list layout.
        fields.append("status = ?")
        values.append(" ".join(str(req.body["status"]).split())[:60])
    if "decoration" in req.body:
        # Taking the hat off is always allowed; putting one on means owning it.
        wanted = str(req.body["decoration"] or "").lower()
        if wanted and wanted not in db.perks_for(req.conn, req.user["id"]):
            raise HttpError(403, "You don't own that.")
        if wanted and not db.SHOP_BY_ID.get(wanted, {}).get("decoration"):
            raise HttpError(400, "That isn't something you can wear.")
        fields.append("decoration = ?")
        values.append(wanted)
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
    return {"user": me_payload(row, db.perks_for(req.conn, row["id"]))}


# ---------------------------------------------------------------- guild API

@route("GET", r"/api/guilds")
def api_guilds(req):
    req.require_auth()
    uid = req.user["id"]
    guilds = []
    perks = db.perks_for(req.conn, uid)
    muted = muted_channels(req.conn, uid)
    rows = req.conn.execute(
        "SELECT g.* FROM guilds g JOIN guild_members m ON m.guild_id = g.id"
        " WHERE m.user_id = ? ORDER BY m.joined_at",
        (uid,),
    ).fetchall()
    for g in rows:
        # Someone who bought a key expects a door in every server they're in,
        # including ones they joined after buying it.
        if "lounge" in perks:
            ensure_lounge(req.conn, g["id"])
        channels = [
            c
            for c in req.conn.execute(
                "SELECT * FROM channels WHERE guild_id = ? AND kind = 'text'"
                " ORDER BY position, id",
                (g["id"],),
            )
            if not locked_of(c) or locked_of(c) in perks
        ]
        guilds.append(
            {
                "id": g["id"],
                "name": g["name"],
                "color": g["color"],
                "iconUrl": f"/uploads/{g['icon']}" if g["icon"] else None,
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
                        "locked": locked_of(c),
                        "muted": c["id"] in muted,
                        "unread": unread_count(req.conn, uid, c["id"]),
                        "mentions": mention_count(req.conn, uid, c["id"]),
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
        # Every server gets the Frontman.
        bot = db.bot_id(req.conn)
        if bot:
            req.conn.execute(
                "INSERT OR IGNORE INTO guild_members (guild_id, user_id, joined_at)"
                " VALUES (?,?,?)",
                (gid, bot, ts),
            )
        for position, chan in enumerate(("general", "random")):
            req.conn.execute(
                "INSERT INTO channels (guild_id, kind, name, position, created_at)"
                " VALUES (?,'text',?,?,?)",
                (gid, chan, position, ts),
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
    # Land at the bottom of the list rather than wherever position 0 sorts.
    last = req.conn.execute(
        "SELECT COALESCE(MAX(position), -1) p FROM channels WHERE guild_id = ?",
        (guild_id,),
    ).fetchone()["p"]
    with req.conn:
        cur = req.conn.execute(
            "INSERT INTO channels (guild_id, kind, name, position, created_at)"
            " VALUES (?,'text',?,?,?)",
            (guild_id, name, last + 1, db.now()),
        )
    return {"channelId": cur.lastrowid}


@route("PATCH", r"/api/guilds/(\d+)/channels/order")
def api_reorder_channels(req, guild_id):
    """Rewrite channel order from a list of ids, owner only."""
    req.require_auth()
    guild_id = int(guild_id)
    owner_only(req, guild_id)

    wanted = req.body.get("order")
    if not isinstance(wanted, list) or not wanted:
        raise HttpError(400, "Send the channel ids in their new order.")

    # Locked channels are left out: they sort to the bottom on their own, and
    # an owner without a key can't see one to drag it anywhere.
    mine = [
        r["id"]
        for r in req.conn.execute(
            "SELECT id FROM channels WHERE guild_id = ? AND kind = 'text'"
            " AND locked = ''",
            (guild_id,),
        )
    ]
    try:
        wanted = [int(c) for c in wanted]
    except (TypeError, ValueError):
        raise HttpError(400, "Channel ids must be numbers.")
    # Must be exactly this server's channels — no additions, no omissions.
    if sorted(wanted) != sorted(mine):
        raise HttpError(400, "That list doesn't match this server's channels.")

    with req.conn:
        for index, channel_id in enumerate(wanted):
            req.conn.execute(
                "UPDATE channels SET position = ? WHERE id = ? AND guild_id = ?",
                (index, channel_id, guild_id),
            )
    return {"ok": True}


@route("POST", r"/api/guilds/(\d+)/icon", raw=images.MAX_AVATAR)
def api_set_guild_icon(req, guild_id):
    req.require_auth()
    guild_id = int(guild_id)
    owner_only(req, guild_id)
    data, kind = read_image_or_400(req, images.MAX_AVATAR)
    stored = store_upload(data, kind)

    previous = req.conn.execute(
        "SELECT icon FROM guilds WHERE id = ?", (guild_id,)
    ).fetchone()["icon"]
    with req.conn:
        req.conn.execute("UPDATE guilds SET icon = ? WHERE id = ?", (stored, guild_id))
    discard_upload(previous)
    return {"iconUrl": f"/uploads/{stored}"}


@route("DELETE", r"/api/guilds/(\d+)/icon")
def api_clear_guild_icon(req, guild_id):
    req.require_auth()
    guild_id = int(guild_id)
    owner_only(req, guild_id)
    previous = req.conn.execute(
        "SELECT icon FROM guilds WHERE id = ?", (guild_id,)
    ).fetchone()["icon"]
    with req.conn:
        req.conn.execute("UPDATE guilds SET icon = NULL WHERE id = ?", (guild_id,))
    discard_upload(previous)
    return {"iconUrl": None}


@route("DELETE", r"/api/channels/(\d+)")
def api_delete_channel(req, channel_id):
    req.require_auth()
    ch = req.conn.execute(
        "SELECT * FROM channels WHERE id = ?", (int(channel_id),)
    ).fetchone()
    if not ch or ch["kind"] != "text":
        raise HttpError(404, "Channel not found.")
    owner_only(req, ch["guild_id"])
    if locked_of(ch):
        raise HttpError(400, "The Sana Lounge belongs to whoever bought a key.")
    remaining = req.conn.execute(
        "SELECT COUNT(*) c FROM channels WHERE guild_id = ? AND kind = 'text'"
        " AND locked = ''",
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
        item = public_user(req.conn, r)
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
    return {"user": me_payload(row, db.perks_for(req.conn, row["id"]))}


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
    return {"user": me_payload(row, db.perks_for(req.conn, row["id"]))}


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
            "iconUrl": f"/uploads/{guild['icon']}" if guild["icon"] else None,
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
        "SELECT id FROM channels WHERE guild_id = ? AND kind = 'text'"
        " ORDER BY position, id LIMIT 1",
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

    reply_to = resolve_reply(req, channel_id)

    with req.conn:
        cur = req.conn.execute(
            "INSERT INTO messages (channel_id, author_id, content, created_at,"
            " sticker_id, reply_to) VALUES (?,?,?,?,?,?)",
            (channel_id, req.user["id"], content, db.now(), sticker_id, reply_to),
        )
    record_mentions(req.conn, ch, cur.lastrowid, content, req.user["id"])
    mark_read(req.conn, req.user["id"], channel_id, cur.lastrowid)
    return {"message": serialize_one(req.conn, cur.lastrowid, req.user["id"])}


def resolve_reply(req, channel_id):
    """Validate a replyTo id, or None. Replies may only target the same channel."""
    raw = req.body.get("replyTo")
    if not raw:
        return None
    parent = req.conn.execute(
        "SELECT id, channel_id FROM messages WHERE id = ?", (int(raw),)
    ).fetchone()
    if not parent:
        raise HttpError(404, "The message you replied to no longer exists.")
    if parent["channel_id"] != channel_id:
        raise HttpError(400, "You can only reply to a message in the same channel.")
    return parent["id"]


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
    drop_messages(req.conn, [msg["id"]])
    return {"ok": True}


MAX_PURGE = 100


def drop_messages(conn, ids):
    """Delete messages and the image files that only they referenced.

    Attachment rows cascade away with the message, but the files on disk would
    otherwise sit there forever.
    """
    if not ids:
        return 0
    marks = ",".join("?" * len(ids))
    files = [
        r["stored_name"]
        for r in conn.execute(
            f"SELECT stored_name FROM attachments WHERE message_id IN ({marks})", ids
        )
    ]
    with conn:
        conn.execute(f"DELETE FROM messages WHERE id IN ({marks})", ids)
    for name in files:
        discard_upload(name)
    return len(ids)


@route("POST", r"/api/channels/(\d+)/purge")
def api_purge(req, channel_id):
    """Bulk-delete the most recent messages in a channel."""
    req.require_auth()
    channel_id = int(channel_id)
    ch = channel_access_or_403(req, channel_id)
    if ch["kind"] != "text":
        raise HttpError(400, "Purge only works in server channels.")
    owner_only(req, ch["guild_id"])

    try:
        count = int(req.body.get("count") or 0)
    except (TypeError, ValueError):
        raise HttpError(400, "Give a number of messages to delete.")
    if count < 1:
        raise HttpError(400, "Give a number of messages to delete.")
    if count > MAX_PURGE:
        raise HttpError(400, f"You can purge at most {MAX_PURGE} messages at a time.")

    ids = [
        r["id"]
        for r in req.conn.execute(
            "SELECT id FROM messages WHERE channel_id = ? ORDER BY id DESC LIMIT ?",
            (channel_id, count),
        )
    ]
    return {"deleted": drop_messages(req.conn, ids)}


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
    ch = channel_access_or_403(req, msg["channel_id"])
    with req.conn:
        req.conn.execute(
            "UPDATE messages SET content = ?, edited_at = ? WHERE id = ?",
            (content, db.now(), msg["id"]),
        )
    record_mentions(req.conn, ch, msg["id"], content, req.user["id"])
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
        "SELECT r.emoji, u.id, u.username, u.color, u.avatar FROM reactions r"
        " JOIN users u ON u.id = r.user_id WHERE r.message_id = ? ORDER BY r.id",
        (msg["id"],),
    ).fetchall()
    grouped = {}
    for r in rows:
        grouped.setdefault(r["emoji"], []).append({
            "id": r["id"],
            "username": r["username"],
            "color": r["color"],
            "avatarUrl": f"/uploads/{r['avatar']}" if r["avatar"] else None,
        })
    return {"reactors": grouped}


# ------------------------------------------------------------------- mutes

def muted_channels(conn, user_id):
    return {
        r["channel_id"]
        for r in conn.execute(
            "SELECT channel_id FROM channel_mutes WHERE user_id = ?", (user_id,)
        )
    }


@route("POST", r"/api/channels/(\d+)/mute")
def api_toggle_mute(req, channel_id):
    """Silence a channel, or bring it back. Unread state is left alone, so
    unmuting shows the badge that was there all along."""
    req.require_auth()
    channel_id = int(channel_id)
    channel_access_or_403(req, channel_id)
    want = req.body.get("muted")
    existing = req.conn.execute(
        "SELECT 1 FROM channel_mutes WHERE user_id = ? AND channel_id = ?",
        (req.user["id"], channel_id),
    ).fetchone()
    muted = (not existing) if want is None else bool(want)
    with req.conn:
        if muted:
            req.conn.execute(
                "INSERT OR IGNORE INTO channel_mutes (user_id, channel_id, created_at)"
                " VALUES (?,?,?)",
                (req.user["id"], channel_id, db.now()),
            )
        else:
            req.conn.execute(
                "DELETE FROM channel_mutes WHERE user_id = ? AND channel_id = ?",
                (req.user["id"], channel_id),
            )
    return {"muted": muted}


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


# ------------------------------------------------------------------- polls

MAX_POLL_OPTIONS = 6
MIN_POLL_OPTIONS = 2
MAX_POLL_QUESTION = 200
MAX_POLL_OPTION = 80


def serialize_poll(conn, row, viewer_id):
    options = json.loads(row["options"])
    votes = conn.execute(
        "SELECT choice, COUNT(*) c,"
        " SUM(CASE WHEN user_id = ? THEN 1 ELSE 0 END) mine"
        " FROM poll_votes WHERE poll_id = ? GROUP BY choice",
        (viewer_id, row["id"]),
    ).fetchall()
    counts = {r["choice"]: r for r in votes}
    total = sum(r["c"] for r in votes)
    voters = conn.execute(
        "SELECT COUNT(DISTINCT user_id) c FROM poll_votes WHERE poll_id = ?", (row["id"],)
    ).fetchone()["c"]
    author = conn.execute(
        "SELECT * FROM users WHERE id = ?", (row["author_id"],)
    ).fetchone()
    return {
        "id": row["id"],
        "question": row["question"],
        "multi": bool(row["multi"]),
        "closed": bool(row["closed"]),
        "isAuthor": row["author_id"] == viewer_id,
        "author": public_user(conn, author) if author else None,
        "voters": voters,
        "total": total,
        "options": [
            {
                "index": i,
                "label": label,
                "votes": counts[i]["c"] if i in counts else 0,
                "mine": bool(counts[i]["mine"]) if i in counts else False,
                # Shares of the vote, not of the voters: with multiple choice
                # allowed the two are different numbers.
                "share": round((counts[i]["c"] if i in counts else 0) * 100 / total)
                         if total else 0,
            }
            for i, label in enumerate(options)
        ],
    }


@route("POST", r"/api/channels/(\d+)/polls")
def api_create_poll(req, channel_id):
    req.require_auth()
    channel_id = int(channel_id)
    ch = channel_access_or_403(req, channel_id)

    question = req.field("question", maxlen=MAX_POLL_QUESTION)
    raw = req.body.get("options")
    if not isinstance(raw, list):
        raise HttpError(400, "Send the poll's options as a list.")
    options = []
    for item in raw:
        label = " ".join(str(item).split())[:MAX_POLL_OPTION]
        if label and label not in options:
            options.append(label)
    if len(options) < MIN_POLL_OPTIONS:
        raise HttpError(400, "A poll needs at least two different options.")
    if len(options) > MAX_POLL_OPTIONS:
        raise HttpError(400, f"A poll can have at most {MAX_POLL_OPTIONS} options.")

    now = db.now()
    with req.conn:
        cur = req.conn.execute(
            "INSERT INTO polls (channel_id, author_id, question, options, multi,"
            " created_at) VALUES (?,?,?,?,?,?)",
            (channel_id, req.user["id"], question, json.dumps(options),
             1 if req.body.get("multi") else 0, now),
        )
        poll_id = cur.lastrowid
        msg = req.conn.execute(
            "INSERT INTO messages (channel_id, author_id, content, created_at, poll_id)"
            " VALUES (?,?,'',?,?)",
            (channel_id, req.user["id"], now, poll_id),
        )
        req.conn.execute(
            "UPDATE polls SET message_id = ? WHERE id = ?", (msg.lastrowid, poll_id)
        )
    mark_read(req.conn, req.user["id"], channel_id, msg.lastrowid)
    return {"message": serialize_one(req.conn, msg.lastrowid, req.user["id"])}


def load_poll(req, poll_id):
    row = req.conn.execute("SELECT * FROM polls WHERE id = ?", (int(poll_id),)).fetchone()
    if not row:
        raise HttpError(404, "That poll no longer exists.")
    channel_access_or_403(req, row["channel_id"])
    return row


@route("POST", r"/api/polls/(\d+)/vote")
def api_vote(req, poll_id):
    """Cast or take back a vote. Clicking your own choice again removes it."""
    req.require_auth()
    row = load_poll(req, poll_id)
    if row["closed"]:
        raise HttpError(409, "That poll is closed.")
    options = json.loads(row["options"])
    try:
        choice = int(req.body.get("choice"))
    except (TypeError, ValueError):
        raise HttpError(400, "Pick one of the options.")
    if not 0 <= choice < len(options):
        raise HttpError(400, "That isn't one of the options.")

    existing = req.conn.execute(
        "SELECT id FROM poll_votes WHERE poll_id = ? AND user_id = ? AND choice = ?",
        (row["id"], req.user["id"], choice),
    ).fetchone()
    with req.conn:
        if existing:
            req.conn.execute("DELETE FROM poll_votes WHERE id = ?", (existing["id"],))
        else:
            # Single-choice polls hold one vote per person, so picking a new
            # option moves the old one rather than stacking on top of it.
            if not row["multi"]:
                req.conn.execute(
                    "DELETE FROM poll_votes WHERE poll_id = ? AND user_id = ?",
                    (row["id"], req.user["id"]),
                )
            req.conn.execute(
                "INSERT INTO poll_votes (poll_id, user_id, choice, created_at)"
                " VALUES (?,?,?,?)",
                (row["id"], req.user["id"], choice, db.now()),
            )
    return {"message": serialize_one(req.conn, row["message_id"], req.user["id"])}


@route("POST", r"/api/polls/(\d+)/close")
def api_close_poll(req, poll_id):
    req.require_auth()
    row = load_poll(req, poll_id)
    if row["author_id"] != req.user["id"]:
        ch = req.conn.execute(
            "SELECT * FROM channels WHERE id = ?", (row["channel_id"],)
        ).fetchone()
        owner = ch["kind"] == "text" and req.conn.execute(
            "SELECT 1 FROM guilds WHERE id = ? AND owner_id = ?",
            (ch["guild_id"], req.user["id"]),
        ).fetchone()
        if not owner:
            raise HttpError(403, "Only whoever started the poll can close it.")
    with req.conn:
        req.conn.execute("UPDATE polls SET closed = 1 WHERE id = ?", (row["id"],))
    return {"message": serialize_one(req.conn, row["message_id"], req.user["id"])}


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
                "user": public_user(req.conn, other),
                "unread": unread_count(req.conn, uid, r["id"]),
                "mentions": mention_count(req.conn, uid, r["id"]),
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


# ----------------------------------------------------------------- GIF API

GIPHY_KEY = os.environ.get("GIPHY_API_KEY", "")
GIPHY_TIMEOUT = 6
# Giphy's own demo key is public and heavily throttled; it is only a fallback
# so the picker degrades to an error message instead of silently breaking.
GIPHY_ENDPOINTS = {
    "search": "https://api.giphy.com/v1/gifs/search",
    "trending": "https://api.giphy.com/v1/gifs/trending",
}


@route("GET", r"/api/gifs")
def api_gifs(req):
    """Proxy Giphy so the API key never reaches a browser.

    Only the handful of fields the picker needs are passed back, and the
    search term is length-capped before it leaves us.
    """
    req.require_auth()
    if not GIPHY_KEY:
        raise HttpError(503, "GIFs aren't configured — set GIPHY_API_KEY on the server.")

    query = (req.query.get("q", [""])[0] or "").strip()[:80]
    kind = "search" if query else "trending"
    params = {
        "api_key": GIPHY_KEY,
        "limit": "24",
        "rating": "pg-13",
        "bundle": "messaging_non_clips",
    }
    if query:
        params["q"] = query

    url = f"{GIPHY_ENDPOINTS[kind]}?{urlencode(params)}"
    try:
        with urllib.request.urlopen(url, timeout=GIPHY_TIMEOUT) as res:
            payload = json.loads(res.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            raise HttpError(502, "Giphy rejected the API key.")
        if exc.code == 429:
            raise HttpError(502, "Giphy is rate limiting us — try again shortly.")
        raise HttpError(502, "Giphy is not responding right now.")
    except urllib.error.URLError as exc:
        # A Python install with no CA bundle can't verify any certificate. It
        # bites on macOS python.org builds and looks exactly like an outage,
        # so say what it actually is.
        if isinstance(getattr(exc, "reason", None), ssl.SSLCertVerificationError):
            raise HttpError(
                502,
                "This server can't verify HTTPS certificates, so Giphy is"
                " unreachable. On macOS run the 'Install Certificates.command'"
                " that ships with Python, or set SSL_CERT_FILE=/etc/ssl/cert.pem.",
            )
        raise HttpError(502, "Giphy is not responding right now.")
    except (TimeoutError, json.JSONDecodeError):
        raise HttpError(502, "Giphy is not responding right now.")

    gifs = []
    for item in payload.get("data", []):
        images = item.get("images") or {}
        still = (images.get("fixed_width_still") or {}).get("url")
        anim = (images.get("fixed_width_downsampled")
                or images.get("fixed_width") or {}).get("url")
        full = (images.get("original") or {}).get("url")
        if not anim or not full:
            continue
        gifs.append({
            "id": item.get("id"),
            "title": (item.get("title") or "GIF")[:120],
            "preview": still or anim,
            "url": anim,
            "full": full,
            "width": int((images.get("fixed_width") or {}).get("width") or 200),
            "height": int((images.get("fixed_width") or {}).get("height") or 200),
        })
    return {"gifs": gifs, "query": query}


MAX_FAVOURITES = 100


@route("GET", r"/api/gifs/favourites")
def api_list_favourites(req):
    req.require_auth()
    rows = req.conn.execute(
        "SELECT * FROM gif_favourites WHERE user_id = ? ORDER BY id DESC",
        (req.user["id"],),
    ).fetchall()
    return {
        "gifs": [
            {"id": r["gif_id"], "title": r["title"],
             "preview": r["preview"], "url": r["url"], "full": r["url"],
             "favourite": True}
            for r in rows
        ]
    }


@route("POST", r"/api/gifs/favourites")
def api_add_favourite(req):
    """Toggle: favouriting one you already saved removes it."""
    req.require_auth()
    gif_id = str(req.field("id", maxlen=120))
    existing = req.conn.execute(
        "SELECT id FROM gif_favourites WHERE user_id = ? AND gif_id = ?",
        (req.user["id"], gif_id),
    ).fetchone()
    if existing:
        with req.conn:
            req.conn.execute("DELETE FROM gif_favourites WHERE id = ?", (existing["id"],))
        return {"favourite": False}

    count = req.conn.execute(
        "SELECT COUNT(*) c FROM gif_favourites WHERE user_id = ?", (req.user["id"],)
    ).fetchone()["c"]
    if count >= MAX_FAVOURITES:
        raise HttpError(400, f"You can save up to {MAX_FAVOURITES} GIFs.")

    url = str(req.field("url", maxlen=500))
    preview = str(req.body.get("preview") or url)[:500]
    if not url.startswith("https://"):
        raise HttpError(400, "That doesn't look like a GIF address.")
    with req.conn:
        req.conn.execute(
            "INSERT INTO gif_favourites (user_id, gif_id, title, preview, url,"
            " created_at) VALUES (?,?,?,?,?,?)",
            (req.user["id"], gif_id, str(req.body.get("title") or "")[:120],
             preview, url, db.now()),
        )
    return {"favourite": True}


# ------------------------------------------------------------ Sana Coin API

def wallet_payload(conn, user_id):
    row = conn.execute(
        "SELECT coins, last_claim, last_work, work_shifts FROM users WHERE id = ?",
        (user_id,),
    ).fetchone()
    st = db.stats_for(conn, user_id)
    perks = db.perks_for(conn, user_id)
    waited = db.now() - row["last_claim"]
    shift = db.work_interval(perks)
    worked = db.now() - row["last_work"]
    return {
        "coins": row["coins"],
        "canClaim": waited >= db.CLAIM_INTERVAL,
        "claimIn": max(0, db.CLAIM_INTERVAL - waited),
        "dailyAmount": db.claim_amount(perks),
        "canWork": worked >= shift,
        "workIn": max(0, shift - worked),
        "workPay": db.work_pay(perks, row["work_shifts"]),
        "workShifts": row["work_shifts"],
        "shiftsToRaise": db.shifts_to_raise(row["work_shifts"]),
        "workOdds": db.WORK_SUCCESS,
        "perks": sorted(perks),
        "lottery": {
            "price": db.LOTTERY_PRICE,
            "odds": db.LOTTERY_ODDS,
            "prize": db.LOTTERY_PRIZE,
            "tickets": db.pending_tickets(conn, user_id),
            "drawIn": db.next_draw_in(),
        },
        "stats": {
            "wins": st["wins"],
            "losses": st["losses"],
            "pushes": st["pushes"],
            "net": st["net"],
            "wagered": st["wagered"],
            "biggestWin": st["biggest_win"],
            "streak": st["streak"],
            "bestStreak": st["best_streak"],
            "rank": db.rank_for(st["wins"]),
            "nextRank": db.next_rank(st["wins"]),
        },
    }


@route("GET", r"/api/wallet")
def api_wallet(req):
    """The wallet, plus any lottery result this player hasn't been told about.

    The draw itself runs on a timer (see draw_loop) and is announced in every
    server's #general. This is the personal half: a toast for the person who
    won, handed over exactly once so it doesn't repeat on the next refresh.
    """
    req.require_auth()
    payload = wallet_payload(req.conn, req.user["id"])
    payload["lotteryResults"] = db.collect_results(req.conn, req.user["id"])
    return payload


def wait_text(seconds):
    hours = seconds // 3600
    mins = (seconds % 3600) // 60
    if hours:
        return f"{hours}h {mins}m"
    return f"{mins}m" if mins else "under a minute"


@route("POST", r"/api/wallet/claim")
def api_claim(req):
    """The daily top-up. One per 24h, so nobody can farm it."""
    req.require_auth()
    row = req.conn.execute(
        "SELECT coins, last_claim FROM users WHERE id = ?", (req.user["id"],)
    ).fetchone()
    waited = db.now() - row["last_claim"]
    if waited < db.CLAIM_INTERVAL:
        when = wait_text(db.CLAIM_INTERVAL - waited)
        raise HttpError(429, f"You've already claimed today. Come back in {when}.")
    amount = db.claim_amount(db.perks_for(req.conn, req.user["id"]))
    with req.conn:
        req.conn.execute(
            "UPDATE users SET coins = coins + ?, last_claim = ? WHERE id = ?",
            (amount, db.now(), req.user["id"]),
        )
    return {"claimed": amount, **wallet_payload(req.conn, req.user["id"])}


# What the Frontman says you did for the money. Purely flavour.
SHIFTS = [
    "dealt a double shift at the blackjack table",
    "swept the floor of the slot hall",
    "counted the chip trays twice, because they didn't add up the first time",
    "polished the roulette wheel until it squeaked",
    "talked a high roller out of a very bad idea",
    "restocked the drinks fridge in the Sana Lounge",
    "carried the cash box across the floor without dropping it",
    "sat in for the croupier's break",
]

# One shift in five ends like this, and ends unpaid.
MISHAPS = [
    "reversed the drinks trolley over the boss's cat. The cat is fine. You are not",
    "lost the float somewhere between the tables and the safe",
    "waved through a man in a false moustache carrying the chip tray",
    "tipped a full drinks tray into the roulette wheel",
    "called last orders four hours early, in front of the boss",
    "were found asleep in the cash office at half past two",
    "set off the fire alarm finding out whether it worked. It worked",
    "dealt an entire shoe face up and only noticed at the end",
    "let the boss's cat into the count room. It sat on the money",
]


@route("POST", r"/api/wallet/work")
def api_work(req):
    """An hourly shift.

    Four times in five it pays; the fifth is a bad day at the office and pays
    nothing. Either way the hour is on the clock, and every five hours earns a
    20% rise until the pay tops out.
    """
    req.require_auth()
    perks = db.perks_for(req.conn, req.user["id"])
    interval = db.work_interval(perks)
    row = req.conn.execute(
        "SELECT last_work, work_shifts FROM users WHERE id = ?", (req.user["id"],)
    ).fetchone()
    waited = db.now() - row["last_work"]
    if waited < interval:
        raise HttpError(429, f"You're on a break. Back in {wait_text(interval - waited)}.")

    shifts = row["work_shifts"]
    rate = db.work_pay(perks, shifts)
    went_well = secrets.SystemRandom().random() < db.WORK_SUCCESS
    earned = rate if went_well else 0

    with req.conn:
        req.conn.execute(
            "UPDATE users SET coins = coins + ?, last_work = ?,"
            " work_shifts = work_shifts + 1 WHERE id = ?",
            (earned, db.now(), req.user["id"]),
        )
    return {
        "earned": earned,
        "ok": went_well,
        "shift": secrets.choice(SHIFTS if went_well else MISHAPS),
        # A rise lands the moment the fifth hour is done, so the card can say so.
        "raised": db.work_pay(perks, shifts + 1) > rate,
        **wallet_payload(req.conn, req.user["id"]),
    }


# ------------------------------------------------------------------ the shop

@route("GET", r"/api/shop")
def api_shop(req):
    req.require_auth()
    owned = db.perks_for(req.conn, req.user["id"])
    held = db.pending_tickets(req.conn, req.user["id"])
    return {
        "items": [
            {
                **item,
                # A repeatable item is never "owned" — you can always buy more.
                "owned": not item.get("repeatable") and item["id"] in owned,
                "held": held if item["id"] == "lottery" else 0,
            }
            for item in db.SHOP_ITEMS
        ],
        "coins": db.balance(req.conn, req.user["id"]),
        "drawIn": db.next_draw_in(),
    }


@route("POST", r"/api/shop/buy")
def api_buy(req):
    """Buy a perk. Permanent, non-refundable, and only ever bought once."""
    req.require_auth()
    item = db.SHOP_BY_ID.get(str(req.body.get("item") or "").lower())
    if not item:
        raise HttpError(404, "The Frontman doesn't sell that.")
    repeatable = bool(item.get("repeatable"))
    if not repeatable and item["id"] in db.perks_for(req.conn, req.user["id"]):
        raise HttpError(409, f"You already own the {item['name'].lower()}.")

    have = db.balance(req.conn, req.user["id"])
    if have < item["price"]:
        short = item["price"] - have
        raise HttpError(400,
                        f"That's {item['price']:,} Sana Coin — you're {short:,} short.")
    with req.conn:
        # Take the money and record the sale together: a crash between the two
        # would either charge for nothing or hand out a free perk.
        req.conn.execute(
            "UPDATE users SET coins = coins - ? WHERE id = ? AND coins >= ?",
            (item["price"], req.user["id"], item["price"]),
        )
        if repeatable:
            db.buy_ticket(req.conn, req.user["id"], item["price"])
        else:
            req.conn.execute(
                "INSERT INTO purchases (user_id, item, price, created_at)"
                " VALUES (?,?,?,?)",
                (req.user["id"], item["id"], item["price"], db.now()),
            )
    if item.get("decoration"):
        # Worn straight away — nobody buys a hat to leave it in the box.
        with req.conn:
            req.conn.execute(
                "UPDATE users SET decoration = ? WHERE id = ?",
                (item["id"], req.user["id"]),
            )

    if item["id"] == "lounge":
        # The key is no use without a door: make sure every server they are in
        # has a lounge to walk into.
        for r in req.conn.execute(
            "SELECT guild_id FROM guild_members WHERE user_id = ?", (req.user["id"],)
        ).fetchall():
            ensure_lounge(req.conn, r["guild_id"])

    out = {"item": item, "wallet": wallet_payload(req.conn, req.user["id"])}
    # Bought from a channel: the Frontman announces it there, the way it
    # announces everything else it is asked to do.
    channel_id = req.body.get("channelId")
    if channel_id:
        channel_access_or_403(req, int(channel_id))
        me = req.conn.execute(
            "SELECT * FROM users WHERE id = ?", (req.user["id"],)
        ).fetchone()
        card = post_info_card(req, int(channel_id), {
            "kind": "purchase",
            "player": public_user(req.conn, me, True),
            "item": item,
            "coins": out["wallet"]["coins"],
        })
        out["message"] = card["message"]
    return out


def ensure_lounge(conn, guild_id):
    """Create the server's Sana Lounge channel if it hasn't got one yet."""
    if not guild_id:
        return None
    row = conn.execute(
        "SELECT id FROM channels WHERE guild_id = ? AND locked = 'lounge'", (guild_id,)
    ).fetchone()
    if row:
        return row["id"]
    with conn:
        cur = conn.execute(
            "INSERT INTO channels (guild_id, kind, name, topic, position, locked,"
            " created_at) VALUES (?,'text',?,?,?,'lounge',?)",
            # Pinned to the bottom by a position no drag can reach: reordering
            # rewrites the visible channels as 0, 1, 2… and skips this one.
            (guild_id, db.LOUNGE_CHANNEL, "Members only. Bought, not given.",
             LOUNGE_POSITION, db.now()),
        )
    return cur.lastrowid


LOUNGE_POSITION = 9999


@route("GET", r"/api/guilds/(\d+)/bank")
def api_bank(req, guild_id):
    """Everyone's balance in this server, richest first."""
    req.require_auth()
    guild_id = int(guild_id)
    guild_member_or_403(req, guild_id)
    rows = req.conn.execute(
        "SELECT u.*, COALESCE(s.wins, 0) wins FROM guild_members m"
        " JOIN users u ON u.id = m.user_id"
        " LEFT JOIN game_stats s ON s.user_id = u.id"
        " WHERE m.guild_id = ? AND u.is_bot = 0"
        " ORDER BY u.coins DESC, u.username COLLATE NOCASE",
        (guild_id,),
    ).fetchall()
    perks = db.perks_map(req.conn, [r["id"] for r in rows])
    return {
        "accounts": [
            {
                "id": r["id"],
                "username": r["username"],
                "color": r["color"],
                "avatarUrl": db.avatar_url(r),
                "perks": sorted(perks.get(r["id"], ())),
                "coins": r["coins"],
                "rank": db.rank_for(r["wins"]),
            }
            for r in rows
        ],
        "total": sum(r["coins"] for r in rows),
    }


@route("POST", r"/api/guilds/(\d+)/reset")
def api_reset_economy(req, guild_id):
    """Wipe balances and records back to day one, for this server's members.

    Sana Coin and game stats are per-account rather than per-server, so this
    reaches every table that person plays at. That is the honest reading of
    "reset the leaderboard" — a score you keep somewhere else isn't reset —
    and the confirmation dialog says so before anything happens.

    Perks bought from the shop are left alone: they were paid for, and a reset
    is meant to level the scoreboard, not confiscate.
    """
    req.require_auth()
    guild_id = int(guild_id)
    owner_only(req, guild_id)

    members = [
        r["user_id"]
        for r in req.conn.execute(
            "SELECT m.user_id FROM guild_members m JOIN users u ON u.id = m.user_id"
            " WHERE m.guild_id = ? AND u.is_bot = 0",
            (guild_id,),
        )
    ]
    if not members:
        return {"reset": 0}
    marks = ",".join("?" * len(members))
    with req.conn:
        req.conn.execute(
            f"UPDATE users SET coins = ?, last_claim = 0, last_work = 0"
            f" WHERE id IN ({marks})",
            [db.STARTING_COINS] + members,
        )
        req.conn.execute(f"DELETE FROM game_stats WHERE user_id IN ({marks})", members)
    return {"reset": len(members), "startingCoins": db.STARTING_COINS}


@route("GET", r"/api/guilds/(\d+)/leaderboard")
def api_leaderboard(req, guild_id):
    req.require_auth()
    guild_id = int(guild_id)
    guild_member_or_403(req, guild_id)
    rows = req.conn.execute(
        "SELECT u.id, u.username, u.color, u.avatar,"
        " COALESCE(s.wins,0) wins, COALESCE(s.losses,0) losses,"
        " COALESCE(s.net,0) net, COALESCE(s.best_streak,0) best_streak"
        " FROM guild_members m JOIN users u ON u.id = m.user_id"
        " LEFT JOIN game_stats s ON s.user_id = u.id"
        " WHERE m.guild_id = ? AND u.is_bot = 0"
        " ORDER BY wins DESC, net DESC, u.username COLLATE NOCASE LIMIT 25",
        (guild_id,),
    ).fetchall()
    perks = db.perks_map(req.conn, [r["id"] for r in rows])
    return {
        "players": [
            {
                "id": r["id"],
                "username": r["username"],
                "color": r["color"],
                "avatarUrl": f"/uploads/{r['avatar']}" if r["avatar"] else None,
                "perks": sorted(perks.get(r["id"], ())),
                "wins": r["wins"],
                "losses": r["losses"],
                "net": r["net"],
                "bestStreak": r["best_streak"],
                "rank": db.rank_for(r["wins"]),
            }
            for r in rows
        ]
    }


# --------------------------------------------------------------- games API

def serialize_game(conn, row, viewer_id, balance=None):
    """Public view of a game, hiding what the viewer isn't allowed to see.

    `balance` is the viewer's own, shown on the card beside the stake. It is
    passed in where a whole channel is being serialized at once, so a page of
    fifty hands doesn't do fifty identical lookups.
    """
    state = json.loads(row["state"])
    mode, status = row["mode"], row["status"]

    if mode == "info":
        return {"id": row["id"], "mode": "info", "status": "finished", **state}

    if balance is None:
        balance = db.balance(conn, viewer_id)

    if mode == "poker":
        return serialize_poker(conn, row, viewer_id, balance)

    if mode == "roulette":
        host = conn.execute("SELECT * FROM users WHERE id = ?", (row["host_id"],)).fetchone()
        return {
            "id": row["id"],
            "mode": "roulette",
            "status": "finished",
            "bet": row["bet"],
            "number": state["number"],
            "colour": state["colour"],
            "betLabel": state["betLabel"],
            "pays": state["pays"],
            "payout": state["payout"],
            "profit": state["profit"],
            "label": state["label"],
            "balance": balance,
            "player": public_user(conn, host) if host else None,
            "yourSeat": "host" if viewer_id == row["host_id"] else None,
        }

    if mode == "slots":
        host = conn.execute("SELECT * FROM users WHERE id = ?", (row["host_id"],)).fetchone()
        return {
            "id": row["id"],
            "mode": "slots",
            "status": "finished",
            "bet": row["bet"],
            "reels": state["reels"],
            "label": state["label"],
            "payout": state["payout"],
            "profit": state["profit"],
            "balance": balance,
            "player": public_user(conn, host) if host else None,
            "yourSeat": "host" if viewer_id == row["host_id"] else None,
        }

    def seat_of(uid):
        if uid == row["host_id"]:
            return "host"
        if row["guest_id"] and uid == row["guest_id"]:
            return "opp"
        return None

    viewer_seat = seat_of(viewer_id)
    people = {}
    for seat, uid in (("host", row["host_id"]), ("opp", row["guest_id"])):
        if uid:
            u = conn.execute("SELECT * FROM users WHERE id = ?", (uid,)).fetchone()
            people[seat] = public_user(conn, u) if u else None
        else:
            people[seat] = None
    if mode == "cpu":
        people["opp"] = {"id": None, "username": "Dealer", "color": db.BOT_COLOR,
                         "avatarUrl": None, "isBot": True}

    seats = {}
    for seat in ("host", "opp"):
        cards = blackjack.visible_hand(state, seat, mode, viewer_seat)
        # The opponent's hand is dealt up front, but nobody may see it until
        # someone actually takes the seat — otherwise you could shop for a
        # good hand before deciding to join.
        if seat == "opp" and status == "waiting":
            cards = ["??"] * len(cards)
        concealed = "??" in cards
        seats[seat] = {
            "user": people[seat],
            "cards": cards,
            # A concealed hand must not leak its total.
            "total": None if concealed else blackjack.hand_value(state["hands"][seat]),
            "busted": (not concealed) and blackjack.is_bust(state["hands"][seat]),
            "stood": state["stood"][seat],
        }

    if mode == "cpu":
        # The player's side is a list: doubling and splitting can turn one
        # hand into as many as four, each with its own stake and outcome.
        hands = blackjack.player_hands(state)
        outcomes = state.get("results") or []
        seats["host"]["hands"] = [
            {
                "cards": hand["cards"],
                "total": blackjack.hand_value(hand["cards"]),
                "busted": blackjack.is_bust(hand["cards"]),
                "stood": hand["stood"],
                "doubled": hand["doubled"],
                "stake": row["bet"] * hand.get("bet", 1),
                "active": index == state.get("active", 0) and status == "playing",
                "result": outcomes[index] if index < len(outcomes) else None,
            }
            for index, hand in enumerate(hands)
        ]

    result = state.get("result")
    outcome = None
    if result:
        winner = result["winner"]
        if winner == "push":
            outcome = "push"
        elif viewer_seat is None:
            outcome = "host" if winner == "host" else "opp"
        else:
            outcome = "win" if winner == viewer_seat else "lose"

    return {
        "id": row["id"],
        "mode": mode,
        "status": status,
        "bet": row["bet"],
        "balance": balance,
        "seats": seats,
        "turn": state.get("turn"),
        "yourSeat": viewer_seat,
        "yourTurn": viewer_seat is not None and state.get("turn") == viewer_seat
                    and status == "playing",
        "canDouble": (mode == "cpu" and viewer_seat == "host" and status == "playing"
                      and blackjack.can_double(state)),
        "canSplit": (mode == "cpu" and viewer_seat == "host" and status == "playing"
                     and blackjack.can_split(state)),
        "canJoin": status == "waiting" and viewer_id != row["host_id"],
        "result": result,
        "outcome": outcome,
    }


MAX_BET = 1_000_000


def take_bet(req, amount):
    """Validate a stake and hold it out of the player's balance."""
    try:
        amount = int(amount or 0)
    except (TypeError, ValueError):
        raise HttpError(400, "That isn't a valid bet.")
    if amount < 0:
        raise HttpError(400, "A bet can't be negative.")
    if amount > MAX_BET:
        raise HttpError(400, f"The most you can stake is {MAX_BET:,}.")
    if amount:
        have = db.balance(req.conn, req.user["id"])
        if amount > have:
            raise HttpError(400,
                            f"You only have {have:,} Sana Coin. Try /claim for a top-up.")
        with req.conn:
            db.adjust_coins(req.conn, req.user["id"], -amount)
    return amount


def settle_bets(conn, row, state):
    """Pay out a finished hand exactly once, and record it in the stats."""
    if row["settled"]:
        return
    bet, mode = row["bet"], row["mode"]
    winner = state["result"]["winner"]

    with conn:
        conn.execute("UPDATE games SET settled = 1 WHERE id = ?", (row["id"],))

        if mode == "cpu":
            # Every hand is paid separately — a split can win one and lose the
            # other — and each carries its own stake once doubled.
            hands = blackjack.player_hands(state)
            results = state.get("results") or [state["result"]]
            gross = staked = 0
            for hand, result in zip(hands, results):
                stake = bet * hand.get("bet", 1)
                staked += stake
                if result["winner"] == "push":
                    gross += stake
                elif result["winner"] == "host":
                    # Blackjack pays 3:2, an ordinary win pays even money. A 21
                    # made out of a split is not a blackjack, as at any table.
                    natural = (len(hands) == 1 and not state.get("split")
                               and blackjack.is_natural(hand["cards"]))
                    gross += int(stake * 2.5) if natural else stake * 2
            db.adjust_coins(conn, row["host_id"], gross)
            # The record follows the cards, not the money: a for-fun hand
            # stakes nothing but still counts as a win towards your rank.
            outcome = {"host": "win", "push": "push"}.get(winner, "lose")
            db.record_result(conn, row["host_id"], outcome, gross - staked, staked)
            return

        # pvp: both staked, so the winner takes the pot.
        host, guest = row["host_id"], row["guest_id"]
        if winner == "push":
            db.adjust_coins(conn, host, bet)
            if guest:
                db.adjust_coins(conn, guest, bet)
            db.record_result(conn, host, "push", 0, bet)
            if guest:
                db.record_result(conn, guest, "push", 0, bet)
            return

        won_by = host if winner == "host" else guest
        lost_by = guest if winner == "host" else host
        if won_by:
            db.adjust_coins(conn, won_by, bet * 2)
            db.record_result(conn, won_by, "win", bet, bet)
        if lost_by:
            db.record_result(conn, lost_by, "lose", -bet, bet)


def post_game_message(req, channel_id, game_id):
    """The bot posts the card that renders the game."""
    bot = db.bot_id(req.conn)
    with req.conn:
        cur = req.conn.execute(
            "INSERT INTO messages (channel_id, author_id, content, created_at, game_id)"
            " VALUES (?,?,'',?,?)",
            (channel_id, bot, db.now(), game_id),
        )
        req.conn.execute(
            "UPDATE games SET message_id = ? WHERE id = ?", (cur.lastrowid, game_id)
        )
    return cur.lastrowid


@route("POST", r"/api/channels/(\d+)/games")
def api_start_game(req, channel_id):
    req.require_auth()
    channel_id = int(channel_id)
    ch = channel_access_or_403(req, channel_id)
    mode = (req.body.get("mode") or "cpu").lower()
    if mode not in ("cpu", "pvp"):
        raise HttpError(400, "Pick either 'cpu' or 'pvp'.")
    if mode == "pvp" and ch["kind"] == "dm":
        raise HttpError(400, "Open a 1v1 in a server channel so others can join.")

    bet = take_bet(req, req.body.get("bet"))

    state = blackjack.new_game(mode)
    if mode == "pvp":
        # Nobody plays until an opponent joins.
        state["turn"] = None
        status = "waiting"
    else:
        status = "finished" if state["result"] else "playing"

    now = db.now()
    with req.conn:
        cur = req.conn.execute(
            "INSERT INTO games (channel_id, mode, status, host_id, state, bet,"
            " created_at, updated_at) VALUES (?,?,?,?,?,?,?,?)",
            (channel_id, mode, status, req.user["id"], json.dumps(state), bet, now, now),
        )
    gid = cur.lastrowid
    if status == "finished":
        row = req.conn.execute("SELECT * FROM games WHERE id = ?", (gid,)).fetchone()
        settle_bets(req.conn, row, state)
    msg_id = post_game_message(req, channel_id, gid)
    mark_read(req.conn, req.user["id"], channel_id, msg_id)
    return {"message": serialize_one(req.conn, msg_id, req.user["id"]),
            "wallet": wallet_payload(req.conn, req.user["id"])}


def load_game(req, game_id):
    row = req.conn.execute("SELECT * FROM games WHERE id = ?", (int(game_id),)).fetchone()
    if not row:
        raise HttpError(404, "That game no longer exists.")
    channel_access_or_403(req, row["channel_id"])
    return row


def save_game(req, row, state, status):
    with req.conn:
        req.conn.execute(
            "UPDATE games SET state = ?, status = ?, version = version + 1,"
            " updated_at = ? WHERE id = ?",
            (json.dumps(state), status, db.now(), row["id"]),
        )
    if status == "finished":
        fresh = req.conn.execute(
            "SELECT * FROM games WHERE id = ?", (row["id"],)
        ).fetchone()
        settle_bets(req.conn, fresh, state)
    return {"message": serialize_one(req.conn, row["message_id"], req.user["id"]),
            "wallet": wallet_payload(req.conn, req.user["id"])}


def serialize_poker(conn, row, viewer_id, balance=None):
    if balance is None:
        balance = db.balance(conn, viewer_id)
    state = json.loads(row["state"])
    people = {}
    for p in state["players"]:
        if p.get("cpu"):
            people[p["userId"]] = {
                "id": p["userId"],
                "username": p.get("name") or "House",
                "color": db.BOT_COLOR,
                "avatarUrl": None,
                "isBot": True,
            }
            continue
        u = conn.execute("SELECT * FROM users WHERE id = ?", (p["userId"],)).fetchone()
        if u:
            people[p["userId"]] = public_user(conn, u)

    done = state["stage"] == "showdown"
    seats = []
    for p in state["players"]:
        mine = p["userId"] == viewer_id
        # Hole cards are yours alone until the showdown.
        if done and not p["folded"]:
            hole = p["hole"]
        elif mine:
            hole = p["hole"]
        else:
            hole = ["??"] * len(p["hole"])
        entry = {
            "user": people.get(p["userId"]),
            "hole": hole,
            "folded": p["folded"],
            "acted": p["acted"],
            "contributed": p["contributed"],
            "street": p.get("street", 0),
            "isYou": mine,
        }
        if done and state["result"]:
            hand = (state["result"].get("hands") or {}).get(str(p["userId"]))
            if hand:
                entry["hand"] = hand
            entry["won"] = p["userId"] in state["result"]["winners"]
        seats.append(entry)

    me = poker.seat(state, viewer_id)
    ante = state["ante"]
    return {
        "id": row["id"],
        "mode": "poker",
        "status": row["status"],
        "stage": state["stage"],
        "board": state["board"],
        "pot": state["pot"],
        "ante": ante,
        "bet": ante,
        "balance": balance,
        "toMatch": state.get("toMatch", 0),
        # What this viewer must put in to keep playing: 0 means they can check.
        "toCall": poker.owed(state, me) if me else 0,
        "minRaise": ante,
        "maxRaise": ante * poker.MAX_RAISE_MULTIPLE,
        "seats": seats,
        "hostId": row["host_id"],
        "isHost": viewer_id == row["host_id"],
        "youArePlaying": me is not None,
        "yourTurn": bool(me and not me["folded"] and not me["acted"]
                         and state["stage"] not in ("waiting", "showdown")),
        "canJoin": state["stage"] == "waiting" and me is None
                   and len(state["players"]) < poker.MAX_PLAYERS,
        "canDeal": viewer_id == row["host_id"] and state["stage"] == "waiting"
                   and len(state["players"]) >= poker.MIN_PLAYERS,
        "result": state["result"],
    }


def post_info_card(req, channel_id, payload):
    """Persist a Frontman info card and post it as a bot message.

    Reusing the games row keeps the card in channel history, the way a real
    bot embed stays where it was posted.
    """
    now = db.now()
    with req.conn:
        cur = req.conn.execute(
            "INSERT INTO games (channel_id, mode, status, host_id, state, bet,"
            " settled, created_at, updated_at) VALUES (?,?,?,?,?,0,1,?,?)",
            (channel_id, "info", "finished", req.user["id"], json.dumps(payload),
             now, now),
        )
    msg_id = post_game_message(req, channel_id, cur.lastrowid)
    mark_read(req.conn, req.user["id"], channel_id, msg_id)
    return {"message": serialize_one(req.conn, msg_id, req.user["id"]),
            "wallet": wallet_payload(req.conn, req.user["id"])}


@route("POST", r"/api/channels/(\d+)/frontman")
def api_frontman_card(req, channel_id):
    """The Frontman answers /claim, /balance, /bank and /leaderboard in-channel."""
    req.require_auth()
    channel_id = int(channel_id)
    ch = channel_access_or_403(req, channel_id)
    kind = (req.body.get("kind") or "").lower()

    if kind == "claim":
        result = api_claim(req)
        me = req.conn.execute(
            "SELECT * FROM users WHERE id = ?", (req.user["id"],)
        ).fetchone()
        return post_info_card(req, channel_id, {
            "kind": "claim",
            "player": public_user(req.conn, me, True),
            "claimed": result["claimed"],
            "coins": result["coins"],
            "stats": result["stats"],
        })

    if kind == "balance":
        w = wallet_payload(req.conn, req.user["id"])
        me = req.conn.execute(
            "SELECT * FROM users WHERE id = ?", (req.user["id"],)
        ).fetchone()
        return post_info_card(req, channel_id, {
            "kind": "balance",
            "player": public_user(req.conn, me, True),
            "coins": w["coins"],
            "canClaim": w["canClaim"],
            "claimIn": w["claimIn"],
            "stats": w["stats"],
        })

    if kind == "work":
        result = api_work(req)
        me = req.conn.execute(
            "SELECT * FROM users WHERE id = ?", (req.user["id"],)
        ).fetchone()
        return post_info_card(req, channel_id, {
            "kind": "work",
            "player": public_user(req.conn, me, True),
            "earned": result["earned"],
            "ok": result["ok"],
            "raised": result["raised"],
            "shift": result["shift"],
            "coins": result["coins"],
            "workIn": result["workIn"],
            "workPay": result["workPay"],
            "workShifts": result["workShifts"],
            "shiftsToRaise": result["shiftsToRaise"],
            "stats": result["stats"],
        })

    if kind == "reset":
        if ch["kind"] != "text":
            raise HttpError(400, "/reset needs a server channel.")
        guild = owner_only(req, ch["guild_id"])
        data = api_reset_economy(req, ch["guild_id"])
        me = req.conn.execute(
            "SELECT * FROM users WHERE id = ?", (req.user["id"],)
        ).fetchone()
        return post_info_card(req, channel_id, {
            "kind": "reset",
            "player": public_user(req.conn, me, True),
            "guild": guild["name"],
            "count": data["reset"],
            "startingCoins": data.get("startingCoins", db.STARTING_COINS),
        })

    if kind in ("bank", "leaderboard"):
        if ch["kind"] != "text":
            raise HttpError(400, f"/{kind} needs a server channel.")
        data = (api_bank(req, ch["guild_id"]) if kind == "bank"
                else api_leaderboard(req, ch["guild_id"]))
        return post_info_card(req, channel_id, {"kind": kind, **data})

    raise HttpError(400, "Unknown Frontman card.")


@route("POST", r"/api/channels/(\d+)/poker")
def api_start_poker(req, channel_id):
    """Open a poker table. Everyone antes the same amount to sit down."""
    req.require_auth()
    channel_id = int(channel_id)
    ch = channel_access_or_403(req, channel_id)
    if ch["kind"] == "dm":
        raise HttpError(400, "Poker needs a server channel so others can join.")

    ante = take_bet(req, req.body.get("bet"))
    if ante < 10:
        if ante:
            with req.conn:
                db.adjust_coins(req.conn, req.user["id"], ante)
        raise HttpError(400, "The minimum ante is 10 Sana Coin.")

    state = poker.new_table(req.user["id"], ante)
    state["pot"] = ante
    state["players"][0]["contributed"] = ante

    # "vs cpu" seats house players and starts straight away.
    bots = req.body.get("bots")
    bots = max(0, min(int(bots), poker.MAX_PLAYERS - 1)) if bots else 0
    status = "waiting"
    for _ in range(bots):
        poker.add_cpu(state)
    if bots:
        poker.deal(state)
        poker.play_cpus(state)
        status = "finished" if state["stage"] == "showdown" else "playing"

    now = db.now()
    with req.conn:
        cur = req.conn.execute(
            "INSERT INTO games (channel_id, mode, status, host_id, state, bet,"
            " created_at, updated_at) VALUES (?,?,?,?,?,?,?,?)",
            (channel_id, "poker", status, req.user["id"], json.dumps(state),
             ante, now, now),
        )
    if status == "finished":
        row = req.conn.execute("SELECT * FROM games WHERE id = ?", (cur.lastrowid,)).fetchone()
        settle_poker(req.conn, row, state)
    msg_id = post_game_message(req, channel_id, cur.lastrowid)
    mark_read(req.conn, req.user["id"], channel_id, msg_id)
    return {"message": serialize_one(req.conn, msg_id, req.user["id"]),
            "wallet": wallet_payload(req.conn, req.user["id"])}


@route("POST", r"/api/games/(\d+)/poker/join")
def api_poker_join(req, game_id):
    req.require_auth()
    row = load_game(req, game_id)
    if row["mode"] != "poker":
        raise HttpError(400, "That isn't a poker table.")
    state = json.loads(row["state"])
    if state["stage"] != "waiting":
        raise HttpError(409, "That hand has already started.")
    if poker.seat(state, req.user["id"]):
        raise HttpError(409, "You're already at that table.")
    if len(state["players"]) >= poker.MAX_PLAYERS:
        raise HttpError(409, f"That table is full ({poker.MAX_PLAYERS} players).")

    ante = state["ante"]
    if db.balance(req.conn, req.user["id"]) < ante:
        raise HttpError(400, f"The ante is {ante:,} Sana Coin and you can't cover it.")

    player = poker.new_player(req.user["id"])
    player["contributed"] = ante
    state["players"].append(player)
    state["pot"] += ante
    with req.conn:
        db.adjust_coins(req.conn, req.user["id"], -ante)
    return save_poker(req, row, state, "waiting")


def save_poker(req, row, state, status):
    with req.conn:
        req.conn.execute(
            "UPDATE games SET state = ?, status = ?, version = version + 1,"
            " updated_at = ? WHERE id = ?",
            (json.dumps(state), status, db.now(), row["id"]),
        )
    if status == "finished":
        settle_poker(req.conn, row, state)
    return {"message": serialize_one(req.conn, row["message_id"], req.user["id"]),
            "wallet": wallet_payload(req.conn, req.user["id"])}


def settle_poker(conn, row, state):
    """Pay the pot out once, and record everyone's result."""
    fresh = conn.execute("SELECT settled FROM games WHERE id = ?", (row["id"],)).fetchone()
    if fresh["settled"]:
        return
    result = state["result"]
    winners = set(result["winners"])
    share = result["share"]
    with conn:
        conn.execute("UPDATE games SET settled = 1 WHERE id = ?", (row["id"],))
        for p in state["players"]:
            # House players have no wallet and don't appear on the leaderboard.
            if p.get("cpu"):
                continue
            uid, staked = p["userId"], p["contributed"]
            if uid in winners:
                db.adjust_coins(conn, uid, share)
                db.record_result(conn, uid, "win", share - staked, staked)
            else:
                db.record_result(conn, uid, "lose", -staked, staked)


@route("POST", r"/api/games/(\d+)/poker/deal")
def api_poker_deal(req, game_id):
    req.require_auth()
    row = load_game(req, game_id)
    if row["mode"] != "poker":
        raise HttpError(400, "That isn't a poker table.")
    if row["host_id"] != req.user["id"]:
        raise HttpError(403, "Only the player who opened the table can deal.")
    state = json.loads(row["state"])
    if state["stage"] != "waiting":
        raise HttpError(409, "The cards are already out.")
    if len(state["players"]) < poker.MIN_PLAYERS:
        raise HttpError(400, "You need at least one other player.")
    poker.deal(state)
    return save_poker(req, row, state, "playing")


def raise_amount(req, state):
    """How much a raise puts on top of the call, in whole antes."""
    ante = state["ante"] or 1
    raw = req.body.get("raise")
    amount = int(raw) if raw else ante
    if amount <= 0:
        raise HttpError(400, "A raise has to be more than nothing.")
    if amount % ante:
        raise HttpError(400, f"Raise in multiples of the {ante:,} ante.")
    if amount > ante * poker.MAX_RAISE_MULTIPLE:
        raise HttpError(
            400, f"The most you can raise at once is {ante * poker.MAX_RAISE_MULTIPLE:,}."
        )
    return amount


@route("POST", r"/api/games/(\d+)/poker/action")
def api_poker_action(req, game_id):
    req.require_auth()
    row = load_game(req, game_id)
    if row["mode"] != "poker":
        raise HttpError(400, "That isn't a poker table.")
    action = (req.body.get("action") or "").lower()
    # 'stay' is the old name for the free-to-continue action; it still arrives
    # from cards posted before betting existed, and means the same thing.
    if action == "stay":
        action = "call"
    if action not in ("check", "call", "raise", "fold"):
        raise HttpError(400, "Action must be 'check', 'call', 'raise' or 'fold'.")

    state = json.loads(row["state"])
    me = poker.seat(state, req.user["id"])
    if not me:
        raise HttpError(403, "You're not at that table.")
    if state["stage"] in ("waiting", "showdown"):
        raise HttpError(409, "There's nothing to act on.")
    if me["folded"] or me["acted"]:
        raise HttpError(409, "You've already acted this round.")

    price = poker.owed(state, me)
    if action == "check" and price:
        raise HttpError(400, f"There's {price:,} to call — call, raise or fold.")

    if action == "fold":
        poker.fold(state, req.user["id"])
    else:
        extra = 0
        if action == "raise":
            extra = raise_amount(req, state)
        cost = price + extra
        if cost > db.balance(req.conn, req.user["id"]):
            raise HttpError(400, f"That costs {cost:,} Sana Coin and you can't cover it.")
        spent = (poker.raise_to(state, req.user["id"], extra) if action == "raise"
                 else poker.stay(state, req.user["id"]))
        if spent:
            with req.conn:
                db.adjust_coins(req.conn, req.user["id"], -spent)

    if poker.hand_over(state) or poker.round_complete(state):
        poker.advance(state)
    poker.play_cpus(state)

    status = "finished" if state["stage"] == "showdown" else "playing"
    return save_poker(req, row, state, status)


@route("POST", r"/api/channels/(\d+)/slots")
def api_slots(req, channel_id):
    """One pull of the slot machine, posted by the bot."""
    req.require_auth()
    channel_id = int(channel_id)
    channel_access_or_403(req, channel_id)

    bet = take_bet(req, req.body.get("bet"))
    if bet < 10:
        # Refund the stake we just held before rejecting.
        if bet:
            with req.conn:
                db.adjust_coins(req.conn, req.user["id"], bet)
        raise HttpError(400, "Minimum spin is 10 Sana Coin.")

    result = slots.play(bet)
    state = {
        "kind": "slots",
        "reels": result["reels"],
        "label": result["label"],
        "payout": result["payout"],
        "profit": result["profit"],
        "result": {"winner": "host" if result["won"] else "opp",
                   "text": result["label"]},
    }
    now = db.now()
    with req.conn:
        cur = req.conn.execute(
            "INSERT INTO games (channel_id, mode, status, host_id, state, bet,"
            " settled, created_at, updated_at) VALUES (?,?,?,?,?,?,1,?,?)",
            (channel_id, "slots", "finished", req.user["id"], json.dumps(state),
             bet, now, now),
        )
        db.adjust_coins(req.conn, req.user["id"], result["payout"])
        outcome = "win" if result["won"] else ("push" if result["pushed"] else "lose")
        db.record_result(req.conn, req.user["id"], outcome, result["profit"], bet)

    msg_id = post_game_message(req, channel_id, cur.lastrowid)
    mark_read(req.conn, req.user["id"], channel_id, msg_id)
    return {"message": serialize_one(req.conn, msg_id, req.user["id"]),
            "wallet": wallet_payload(req.conn, req.user["id"])}


@route("POST", r"/api/channels/(\d+)/roulette")
def api_roulette(req, channel_id):
    """One spin of the wheel, posted by the bot."""
    req.require_auth()
    channel_id = int(channel_id)
    channel_access_or_403(req, channel_id)

    kind = (req.body.get("kind") or "").lower()
    pick = req.body.get("number")
    try:
        pick = int(pick) if pick is not None and pick != "" else None
    except (TypeError, ValueError):
        raise HttpError(400, "Pick a number between 0 and 36.")
    if not roulette.valid(kind, pick):
        raise HttpError(400, "Pick a bet from the table, or a number from 0 to 36.")

    bet = take_bet(req, req.body.get("bet"))
    if bet < 10:
        # Refund the stake we just held before rejecting.
        if bet:
            with req.conn:
                db.adjust_coins(req.conn, req.user["id"], bet)
        raise HttpError(400, "Minimum spin is 10 Sana Coin.")

    result = roulette.play(kind, bet, pick)
    state = {
        "kind": "roulette",
        "number": result["number"],
        "colour": result["colour"],
        "betLabel": result["bet"],
        "pays": result["pays"],
        "payout": result["payout"],
        "profit": result["profit"],
        "label": result["label"],
        "result": {"winner": "host" if result["won"] else "opp",
                   "text": result["label"]},
    }
    now = db.now()
    with req.conn:
        cur = req.conn.execute(
            "INSERT INTO games (channel_id, mode, status, host_id, state, bet,"
            " settled, created_at, updated_at) VALUES (?,?,?,?,?,?,1,?,?)",
            (channel_id, "roulette", "finished", req.user["id"], json.dumps(state),
             bet, now, now),
        )
        db.adjust_coins(req.conn, req.user["id"], result["payout"])
        db.record_result(req.conn, req.user["id"], "win" if result["won"] else "lose",
                         result["profit"], bet)

    msg_id = post_game_message(req, channel_id, cur.lastrowid)
    mark_read(req.conn, req.user["id"], channel_id, msg_id)
    return {"message": serialize_one(req.conn, msg_id, req.user["id"]),
            "wallet": wallet_payload(req.conn, req.user["id"])}


@route("POST", r"/api/games/(\d+)/join")
def api_join_game(req, game_id):
    req.require_auth()
    row = load_game(req, game_id)
    if row["mode"] != "pvp":
        raise HttpError(400, "That game isn't a 1v1.")
    if row["status"] != "waiting":
        raise HttpError(409, "Someone already joined that game.")
    if row["host_id"] == req.user["id"]:
        raise HttpError(400, "You can't join your own game — wait for someone else.")

    # Joining means matching the host's stake.
    bet = row["bet"]
    if bet:
        have = db.balance(req.conn, req.user["id"])
        if have < bet:
            raise HttpError(400,
                            f"That table is {bet:,} Sana Coin and you have {have:,}.")

    state = json.loads(row["state"])
    state["turn"] = "host"
    with req.conn:
        req.conn.execute(
            "UPDATE games SET guest_id = ?, status = 'playing', state = ?,"
            " version = version + 1, updated_at = ? WHERE id = ? AND status = 'waiting'",
            (req.user["id"], json.dumps(state), db.now(), row["id"]),
        )
    # Someone else may have won the race to join.
    fresh = req.conn.execute("SELECT * FROM games WHERE id = ?", (row["id"],)).fetchone()
    if fresh["guest_id"] != req.user["id"]:
        raise HttpError(409, "Someone else joined first.")
    # Only take the stake once the seat is definitely ours.
    if bet:
        with req.conn:
            db.adjust_coins(req.conn, req.user["id"], -bet)
    return {"message": serialize_one(req.conn, row["message_id"], req.user["id"]),
            "wallet": wallet_payload(req.conn, req.user["id"])}


@route("POST", r"/api/games/(\d+)/action")
def api_game_action(req, game_id):
    req.require_auth()
    row = load_game(req, game_id)
    action = (req.body.get("action") or "").lower()
    if action not in ("hit", "stand", "double", "split"):
        raise HttpError(400, "Action must be 'hit', 'stand', 'double' or 'split'.")
    if row["status"] != "playing":
        raise HttpError(409, "That hand isn't in play.")

    seat = ("host" if req.user["id"] == row["host_id"]
            else "opp" if row["guest_id"] and req.user["id"] == row["guest_id"] else None)
    if seat is None:
        raise HttpError(403, "You're not in this game.")

    state = json.loads(row["state"])
    if not blackjack.can_act(state, seat):
        raise HttpError(409, "It isn't your turn.")

    if action in ("double", "split"):
        # Both put a second stake on the table, so both need covering first.
        if row["mode"] != "cpu":
            raise HttpError(400, "Doubling and splitting are for hands against the dealer.")
        allowed = (blackjack.can_double(state) if action == "double"
                   else blackjack.can_split(state))
        if not allowed:
            raise HttpError(409, "You can't do that with this hand.")
        extra = row["bet"] * blackjack.current_hand(state)["bet"] if action == "double" \
            else row["bet"]
        if extra:
            have = db.balance(req.conn, req.user["id"])
            if extra > have:
                raise HttpError(400, f"That needs another {extra:,} and you have {have:,}.")
            with req.conn:
                db.adjust_coins(req.conn, req.user["id"], -extra)
        if action == "double":
            blackjack.double(state)
        else:
            blackjack.split(state)
    elif action == "hit":
        blackjack.hit(state, seat, row["mode"])
    else:
        blackjack.stand(state, seat, row["mode"])

    status = "finished" if state.get("result") else "playing"
    return save_game(req, row, state, status)


@route("GET", r"/api/channels/(\d+)/mentionable")
def api_mentionable(req, channel_id):
    """People you can @ in this channel — server members, or the DM partner."""
    req.require_auth()
    channel_id = int(channel_id)
    ch = channel_access_or_403(req, channel_id)
    if ch["kind"] == "dm":
        rows = req.conn.execute(
            "SELECT u.* FROM dm_participants p JOIN users u ON u.id = p.user_id"
            " WHERE p.channel_id = ?",
            (channel_id,),
        ).fetchall()
    else:
        rows = req.conn.execute(
            "SELECT u.* FROM guild_members m JOIN users u ON u.id = m.user_id"
            " WHERE m.guild_id = ? AND u.is_bot = 0"
            " ORDER BY u.username COLLATE NOCASE",
            (ch["guild_id"],),
        ).fetchall()
    # Bots are left out: pinging one does nothing, so suggesting it is noise.
    return {
        "users": [public_user(req.conn, r) for r in rows],
        "everyone": ch["kind"] != "dm",
    }


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
                "user": public_user(req.conn, other) if other else None,
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
    payload = public_user(req.conn, row)
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


# ------------------------------------------------------------- the draw

DRAW_CHECK_EVERY = 30.0     # seconds between "is a draw due?" checks


def announcement_text(conn, draw):
    """What the Frontman says in #general when a draw has run."""
    winners = db.draw_winners(conn, draw["draw_at"])
    entries = db.draw_entries(conn, draw["draw_at"])
    tickets = entries["tickets"] or 0
    players = entries["players"] or 0
    pool = (f"{tickets} ticket{'' if tickets == 1 else 's'} from "
            f"{players} {'person' if players == 1 else 'people'}")

    if not winners:
        return (f"@everyone Tonight's lottery: {pool}, and not one of them came "
                f"up. The {db.LOTTERY_PRIZE:,} rolls over to nobody — it's a "
                f"fresh {db.LOTTERY_ODDS} to 1 tomorrow. Tickets are "
                f"{db.LOTTERY_PRICE:,} from /shop.")

    # Winners are named by full tag, so they get a ping of their own on top of
    # the @everyone. Long lists are capped — a message has a length limit.
    if len(winners) == 1:
        only = winners[0]
        held = only["tickets"]
        across = f" on {held} tickets" if held > 1 else ""
        body = (f"@{only['username']}#{only['discriminator']} takes "
                f"{only['won']:,}{across}")
    else:
        named = [f"@{w['username']}#{w['discriminator']} — {w['won']:,}"
                 for w in winners[:8]]
        more = "" if len(winners) <= 8 else f", and {len(winners) - 8} more"
        body = "Winners: " + ", ".join(named) + more

    return (f"@everyone Tonight's lottery is drawn — {pool}. {body}. "
            f"Tickets for tomorrow are {db.LOTTERY_PRICE:,} from /shop.")


def announce_draw(conn, draw):
    """Post the result to every server's #general, as the Frontman."""
    text = announcement_text(conn, draw)[:MAX_MESSAGE]
    bot = db.bot_id(conn)
    if not bot:
        return
    for guild in conn.execute("SELECT id FROM guilds").fetchall():
        channel = conn.execute(
            # #general by name, or the top of the list if a server hasn't got
            # one. Never a locked channel: most people can't even see it.
            "SELECT * FROM channels WHERE guild_id = ? AND kind = 'text'"
            " AND locked = '' ORDER BY (name <> 'general'), position, id LIMIT 1",
            (guild["id"],),
        ).fetchone()
        if not channel:
            continue
        with conn:
            cur = conn.execute(
                "INSERT INTO messages (channel_id, author_id, content, created_at)"
                " VALUES (?,?,?,?)",
                (channel["id"], bot, text, db.now()),
            )
        # Without this the @everyone renders as a pill but pings nobody.
        record_mentions(conn, channel, cur.lastrowid, text, bot)
    db.mark_announced(conn, draw["draw_at"])


def draw_tick(conn):
    """Run a due draw, if there is one, and announce anything unannounced.

    Announcing is a separate step from drawing so a crash between the two
    doesn't swallow the result: the draw is recorded, and the next tick
    finds it still unannounced and posts it.
    """
    due = db.due_draw(conn)
    if due is not None:
        db.run_draw(conn, due)
    for draw in db.unannounced_draws(conn):
        announce_draw(conn, draw)


def draw_loop(stop):
    """Watch the clock so the lottery is drawn at six whether or not anyone
    is looking. A draw missed while the server was down runs on the next
    boot — late, but never skipped."""
    while not stop.wait(DRAW_CHECK_EVERY):
        conn = None
        try:
            conn = db.connect()
            draw_tick(conn)
        except Exception as err:                       # never kill the thread
            print(f"lottery draw failed: {err!r}", flush=True)
        finally:
            if conn:
                conn.close()


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


def report_storage(existed_before):
    """Say plainly whether this deploy is keeping data or starting empty.

    On a hosted container the filesystem is replaced on every deploy, so the
    database has to live on a mounted volume. Getting that wrong silently
    deletes every account, which is worth shouting about in the deploy log.
    """
    hosted = bool(
        os.environ.get("RAILWAY_ENVIRONMENT")
        or os.environ.get("RAILWAY_PUBLIC_DOMAIN")
        or os.environ.get("RENDER")
    )
    volume = os.environ.get("RAILWAY_VOLUME_MOUNT_PATH")
    db_path = os.path.abspath(db.DB_PATH)

    print(f"Database: {db_path}")
    print(f"Uploads:  {os.path.abspath(db.UPLOAD_DIR)}")
    print("Existing data found." if existed_before else "Started a new, empty database.")

    if not hosted:
        return
    if not volume:
        print("\n  *** WARNING: no volume is attached. ***")
        print("  Everything here is deleted on the next deploy.")
        print("  Attach a volume and set NEXUS_DB to a path inside it.")
    elif not db_path.startswith(os.path.abspath(volume)):
        print(f"\n  *** WARNING: the database is outside the volume ({volume}). ***")
        print("  Everything here is deleted on the next deploy.")
        print(f"  Set NEXUS_DB to something like {os.path.join(volume, 'nexus.db')}")
    elif not existed_before:
        print(f"\n  Note: the volume at {volume} was empty, so this is a fresh start.")
    else:
        print(f"Storage: persistent volume at {volume} — data carries across deploys.")


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

    existed = os.path.exists(db.DB_PATH)
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
    print()
    report_storage(existed)
    print(f"  Lottery draw:      {db.draw_clock()} daily"
          f" (next in {wait_text(db.next_draw_in())})")
    print(flush=True)

    # Daemon, so Ctrl-C doesn't have to wait for it to come round again.
    stop = threading.Event()
    threading.Thread(target=draw_loop, args=(stop,), daemon=True).start()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down.")
        stop.set()
        server.server_close()


if __name__ == "__main__":
    main()
