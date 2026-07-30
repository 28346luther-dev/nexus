"""SQLite storage layer. Stdlib only."""

import os
import re
import secrets
import sqlite3
import hashlib
import time

# NEXUS_DB lets you point a second instance at a throwaway database, so tests
# never touch real accounts and messages.
DB_PATH = os.environ.get("NEXUS_DB") or os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "data.db"
)

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    username      TEXT NOT NULL,
    discriminator TEXT NOT NULL,
    email         TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    salt          TEXT NOT NULL,
    color         TEXT NOT NULL,
    bio           TEXT NOT NULL DEFAULT '',
    created_at    INTEGER NOT NULL,
    last_seen     INTEGER NOT NULL DEFAULT 0,
    UNIQUE (username, discriminator)
);

CREATE TABLE IF NOT EXISTS sessions (
    token      TEXT PRIMARY KEY,
    user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at INTEGER NOT NULL,
    last_used  INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS guilds (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT NOT NULL,
    owner_id   INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    color      TEXT NOT NULL,
    created_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS guild_members (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id  INTEGER NOT NULL REFERENCES guilds(id) ON DELETE CASCADE,
    user_id   INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    joined_at INTEGER NOT NULL,
    UNIQUE (guild_id, user_id)
);

-- kind: 'text' for guild channels, 'dm' for direct messages
CREATE TABLE IF NOT EXISTS channels (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id   INTEGER REFERENCES guilds(id) ON DELETE CASCADE,
    kind       TEXT NOT NULL DEFAULT 'text',
    name       TEXT NOT NULL,
    topic      TEXT NOT NULL DEFAULT '',
    created_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS dm_participants (
    channel_id INTEGER NOT NULL REFERENCES channels(id) ON DELETE CASCADE,
    user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    UNIQUE (channel_id, user_id)
);

CREATE TABLE IF NOT EXISTS messages (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    channel_id INTEGER NOT NULL REFERENCES channels(id) ON DELETE CASCADE,
    author_id  INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    content    TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    edited_at  INTEGER
);
CREATE INDEX IF NOT EXISTS idx_messages_channel ON messages(channel_id, id);

CREATE TABLE IF NOT EXISTS invites (
    code       TEXT PRIMARY KEY,
    guild_id   INTEGER NOT NULL REFERENCES guilds(id) ON DELETE CASCADE,
    creator_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    uses       INTEGER NOT NULL DEFAULT 0,
    max_uses   INTEGER,
    expires_at INTEGER,
    created_at INTEGER NOT NULL
);

-- status: 'pending' | 'accepted'
CREATE TABLE IF NOT EXISTS friendships (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    requester_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    addressee_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    status       TEXT NOT NULL,
    created_at   INTEGER NOT NULL,
    UNIQUE (requester_id, addressee_id)
);

CREATE TABLE IF NOT EXISTS read_state (
    user_id         INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    channel_id      INTEGER NOT NULL REFERENCES channels(id) ON DELETE CASCADE,
    last_read_msg   INTEGER NOT NULL DEFAULT 0,
    UNIQUE (user_id, channel_id)
);
"""

COLORS = [
    "#5865f2", "#57f287", "#fee75c", "#eb459e", "#ed4245",
    "#3ba55d", "#faa61a", "#9b59b6", "#1abc9c", "#e67e22",
]


def connect():
    conn = sqlite3.connect(DB_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


# How long a session survives without being used, and how often we bump
# last_used / re-issue the cookie. Sliding, so an active user is never
# logged out mid-conversation.
SESSION_IDLE_TTL = 60 * 60 * 24 * 30      # 30 days
SESSION_TOUCH_EVERY = 60 * 60             # 1 hour


def init():
    # NEXUS_DB usually points into a mounted volume (e.g. /data/nexus.db) that
    # may not exist yet on a fresh deploy.
    parent = os.path.dirname(DB_PATH)
    if parent:
        os.makedirs(parent, exist_ok=True)
    conn = connect()
    with conn:
        conn.executescript(SCHEMA)
        migrate(conn)
        # Drop sessions nobody has touched in a month.
        conn.execute(
            "DELETE FROM sessions WHERE last_used > 0 AND last_used < ?",
            (now() - SESSION_IDLE_TTL,),
        )
    conn.close()


def migrate(conn):
    """Add columns introduced after a database was first created."""
    have = {r["name"] for r in conn.execute("PRAGMA table_info(sessions)")}
    if "last_used" not in have:
        conn.execute("ALTER TABLE sessions ADD COLUMN last_used INTEGER NOT NULL DEFAULT 0")
        conn.execute("UPDATE sessions SET last_used = created_at")


def now():
    return int(time.time())


# ---------------------------------------------------------------- passwords

def hash_password(password, salt=None):
    salt = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 200_000)
    return digest.hex(), salt


def verify_password(password, salt, expected):
    candidate, _ = hash_password(password, salt)
    return secrets.compare_digest(candidate, expected)


# ---------------------------------------------------------------- users

USERNAME_RE = re.compile(r"^[A-Za-z0-9_.\- ]{2,32}$")
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def pick_discriminator(conn, username):
    """Find a free #0000 tag for this username."""
    taken = {
        r["discriminator"]
        for r in conn.execute(
            "SELECT discriminator FROM users WHERE username = ?", (username,)
        )
    }
    if len(taken) >= 9999:
        return None
    while True:
        tag = f"{secrets.randbelow(9999) + 1:04d}"
        if tag not in taken:
            return tag


def create_user(conn, username, email, password):
    pw_hash, salt = hash_password(password)
    tag = pick_discriminator(conn, username)
    if tag is None:
        raise ValueError("That username is full — pick another.")
    color = secrets.choice(COLORS)
    cur = conn.execute(
        "INSERT INTO users (username, discriminator, email, password_hash, salt,"
        " color, created_at, last_seen) VALUES (?,?,?,?,?,?,?,?)",
        (username, tag, email.lower(), pw_hash, salt, color, now(), now()),
    )
    return cur.lastrowid


def public_user(row, online=False):
    return {
        "id": row["id"],
        "username": row["username"],
        "discriminator": row["discriminator"],
        "tag": f'{row["username"]}#{row["discriminator"]}',
        "color": row["color"],
        "bio": row["bio"] if "bio" in row.keys() else "",
        "online": online,
    }


ONLINE_WINDOW = 70  # seconds since last poll


def is_online(row):
    try:
        return (now() - (row["last_seen"] or 0)) < ONLINE_WINDOW
    except (IndexError, KeyError):
        return False
