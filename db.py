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

# Uploaded images sit next to the database by default. On Railway that means
# they land inside the mounted volume alongside nexus.db and survive redeploys;
# anywhere else they would be wiped with the container.
UPLOAD_DIR = os.environ.get("NEXUS_UPLOADS") or os.path.join(
    os.path.dirname(os.path.abspath(DB_PATH)) or ".", "uploads"
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

-- Images attached to a message. `stored_name` is a random name we generate;
-- the browser's filename is kept only for display and downloads.
CREATE TABLE IF NOT EXISTS attachments (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id    INTEGER NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
    stored_name   TEXT NOT NULL,
    original_name TEXT NOT NULL,
    mime          TEXT NOT NULL,
    size          INTEGER NOT NULL,
    width         INTEGER,
    height        INTEGER,
    created_at    INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_attachments_message ON attachments(message_id);

-- A blackjack hand posted by the Gamesman bot. `state` is the JSON blob from
-- blackjack.py; the message it is attached to renders it as a card.
CREATE TABLE IF NOT EXISTS games (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    channel_id INTEGER NOT NULL REFERENCES channels(id) ON DELETE CASCADE,
    message_id INTEGER REFERENCES messages(id) ON DELETE CASCADE,
    mode       TEXT NOT NULL,        -- 'cpu' | 'pvp'
    status     TEXT NOT NULL,        -- 'waiting' | 'playing' | 'finished'
    host_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    guest_id   INTEGER REFERENCES users(id) ON DELETE CASCADE,
    state      TEXT NOT NULL,
    -- Bumped on every move. updated_at is only second-granularity, so two
    -- quick actions could look identical and the other player's board
    -- would never refresh.
    version    INTEGER NOT NULL DEFAULT 0,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_games_channel ON games(channel_id, id);

-- Who a message pings. Written when the message is sent, so unread-mention
-- counts stay cheap and survive the author editing the text later.
CREATE TABLE IF NOT EXISTS mentions (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id INTEGER NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
    user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    UNIQUE (message_id, user_id)
);
CREATE INDEX IF NOT EXISTS idx_mentions_user ON mentions(user_id, message_id);

CREATE TABLE IF NOT EXISTS reactions (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id INTEGER NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
    user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    emoji      TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    UNIQUE (message_id, user_id, emoji)
);
CREATE INDEX IF NOT EXISTS idx_reactions_message ON reactions(message_id);

-- Per-server custom stickers. Removing one only sets `archived`, so messages
-- that already used it keep rendering; a partial index lets the freed name be
-- reused straight away.
CREATE TABLE IF NOT EXISTS stickers (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id    INTEGER NOT NULL REFERENCES guilds(id) ON DELETE CASCADE,
    name        TEXT NOT NULL,
    stored_name TEXT NOT NULL,
    mime        TEXT NOT NULL,
    creator_id  INTEGER REFERENCES users(id) ON DELETE SET NULL,
    created_at  INTEGER NOT NULL,
    archived    INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_stickers_guild ON stickers(guild_id);

-- Saved GIFs. Only the URLs Giphy gave us are kept; nothing is re-hosted.
CREATE TABLE IF NOT EXISTS gif_favourites (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    gif_id     TEXT NOT NULL,
    title      TEXT NOT NULL DEFAULT '',
    preview    TEXT NOT NULL,
    url        TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    UNIQUE (user_id, gif_id)
);
CREATE INDEX IF NOT EXISTS idx_gif_fav_user ON gif_favourites(user_id, id DESC);

-- Running totals per player for the Gamesman leaderboard.
CREATE TABLE IF NOT EXISTS game_stats (
    user_id     INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    wins        INTEGER NOT NULL DEFAULT 0,
    losses      INTEGER NOT NULL DEFAULT 0,
    pushes      INTEGER NOT NULL DEFAULT 0,
    wagered     INTEGER NOT NULL DEFAULT 0,
    net         INTEGER NOT NULL DEFAULT 0,
    biggest_win INTEGER NOT NULL DEFAULT 0,
    streak      INTEGER NOT NULL DEFAULT 0,
    best_streak INTEGER NOT NULL DEFAULT 0
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_stickers_name
    ON stickers(guild_id, name) WHERE archived = 0;
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
    os.makedirs(UPLOAD_DIR, exist_ok=True)
    conn = connect()
    with conn:
        conn.executescript(SCHEMA)
        migrate(conn)
        ensure_bot(conn)
        # Drop sessions nobody has touched in a month.
        conn.execute(
            "DELETE FROM sessions WHERE last_used > 0 AND last_used < ?",
            (now() - SESSION_IDLE_TTL,),
        )
    conn.close()


# ---------------------------------------------------------------- Sana Coin

STARTING_COINS = 2000
DAILY_CLAIM = 1000
CLAIM_INTERVAL = 60 * 60 * 24

# Rank is earned by wins, not by balance, so it can't simply be bought.
RANKS = [
    (0, "Rookie"),
    (5, "Chancer"),
    (15, "Hustler"),
    (40, "Sharp"),
    (80, "High Roller"),
    (150, "Sana Legend"),
]


def rank_for(wins):
    name = RANKS[0][1]
    for threshold, label in RANKS:
        if wins >= threshold:
            name = label
    return name


def next_rank(wins):
    """(label, wins still needed) for the next tier, or None at the top."""
    for threshold, label in RANKS:
        if wins < threshold:
            return {"name": label, "winsAway": threshold - wins}
    return None


def balance(conn, user_id):
    row = conn.execute("SELECT coins FROM users WHERE id = ?", (user_id,)).fetchone()
    return row["coins"] if row else 0


def adjust_coins(conn, user_id, delta):
    """Move a player's balance, never below zero."""
    conn.execute(
        "UPDATE users SET coins = MAX(0, coins + ?) WHERE id = ?", (delta, user_id)
    )


def stats_for(conn, user_id):
    conn.execute("INSERT OR IGNORE INTO game_stats (user_id) VALUES (?)", (user_id,))
    return conn.execute(
        "SELECT * FROM game_stats WHERE user_id = ?", (user_id,)
    ).fetchone()


def record_result(conn, user_id, outcome, profit, wagered):
    """Fold one finished hand into a player's running totals.

    `profit` is the net change: positive when they came out ahead.
    """
    stats_for(conn, user_id)
    if outcome == "win":
        conn.execute(
            "UPDATE game_stats SET wins = wins + 1, streak = streak + 1,"
            " best_streak = MAX(best_streak, streak + 1),"
            " biggest_win = MAX(biggest_win, ?) WHERE user_id = ?",
            (profit, user_id),
        )
    elif outcome == "lose":
        conn.execute(
            "UPDATE game_stats SET losses = losses + 1, streak = 0 WHERE user_id = ?",
            (user_id,),
        )
    else:
        conn.execute(
            "UPDATE game_stats SET pushes = pushes + 1 WHERE user_id = ?", (user_id,)
        )
    conn.execute(
        "UPDATE game_stats SET wagered = wagered + ?, net = net + ? WHERE user_id = ?",
        (wagered, profit, user_id),
    )


BOT_NAME = "Gamesman"
BOT_TAG = "0000"
BOT_EMAIL = "gamesman@nexus.bot"
BOT_COLOR = "#3ba55d"


def ensure_bot(conn):
    """Create the Gamesman account and put it in every server.

    It owns no password anyone can use — the hash is random bytes and login
    refuses bot accounts outright — but it needs a real users row so its
    messages, avatar and member-list entry work like anyone else's.
    """
    row = conn.execute("SELECT id FROM users WHERE email = ?", (BOT_EMAIL,)).fetchone()
    if row:
        bot_id = row["id"]
    else:
        pw_hash, salt = hash_password(secrets.token_hex(32))
        cur = conn.execute(
            "INSERT INTO users (username, discriminator, email, password_hash, salt,"
            " color, bio, created_at, last_seen, is_bot)"
            " VALUES (?,?,?,?,?,?,?,?,?,1)",
            (BOT_NAME, BOT_TAG, BOT_EMAIL, pw_hash, salt, BOT_COLOR,
             "Deals blackjack. Type /blackjack to play.", now(), now()),
        )
        bot_id = cur.lastrowid

    # Join any server it isn't in yet, including ones made before it existed.
    conn.execute(
        "INSERT OR IGNORE INTO guild_members (guild_id, user_id, joined_at)"
        " SELECT g.id, ?, ? FROM guilds g"
        " WHERE NOT EXISTS (SELECT 1 FROM guild_members m"
        "                   WHERE m.guild_id = g.id AND m.user_id = ?)",
        (bot_id, now(), bot_id),
    )
    return bot_id


def bot_id(conn):
    row = conn.execute("SELECT id FROM users WHERE email = ?", (BOT_EMAIL,)).fetchone()
    return row["id"] if row else None


def migrate(conn):
    """Add columns introduced after a database was first created."""
    have = {r["name"] for r in conn.execute("PRAGMA table_info(sessions)")}
    if "last_used" not in have:
        conn.execute("ALTER TABLE sessions ADD COLUMN last_used INTEGER NOT NULL DEFAULT 0")
        conn.execute("UPDATE sessions SET last_used = created_at")

    have = {r["name"] for r in conn.execute("PRAGMA table_info(users)")}
    if "avatar" not in have:
        # Stored upload name, or NULL to fall back to the coloured initial.
        conn.execute("ALTER TABLE users ADD COLUMN avatar TEXT")
    if "is_bot" not in have:
        conn.execute("ALTER TABLE users ADD COLUMN is_bot INTEGER NOT NULL DEFAULT 0")
    if "status" not in have:
        # Short free-text line shown under the name, like a Discord status.
        conn.execute("ALTER TABLE users ADD COLUMN status TEXT NOT NULL DEFAULT ''")
    if "coins" not in have:
        # Sana Coin. Everyone starts with a stake so a new account can play.
        conn.execute(
            f"ALTER TABLE users ADD COLUMN coins INTEGER NOT NULL DEFAULT {STARTING_COINS}"
        )
        conn.execute("ALTER TABLE users ADD COLUMN last_claim INTEGER NOT NULL DEFAULT 0")

    have = {r["name"] for r in conn.execute("PRAGMA table_info(games)")}
    if "bet" not in have:
        conn.execute("ALTER TABLE games ADD COLUMN bet INTEGER NOT NULL DEFAULT 0")
        # Set once winnings are paid, so a replayed row can't pay out twice.
        conn.execute("ALTER TABLE games ADD COLUMN settled INTEGER NOT NULL DEFAULT 0")

    have = {r["name"] for r in conn.execute("PRAGMA table_info(messages)")}
    if "game_id" not in have:
        conn.execute(
            "ALTER TABLE messages ADD COLUMN game_id INTEGER"
            " REFERENCES games(id) ON DELETE CASCADE"
        )

    have = {r["name"] for r in conn.execute("PRAGMA table_info(messages)")}
    if "sticker_id" not in have:
        # A sticker message carries no text; the sticker is the whole message.
        conn.execute(
            "ALTER TABLE messages ADD COLUMN sticker_id INTEGER"
            " REFERENCES stickers(id) ON DELETE SET NULL"
        )
    if "reply_to" not in have:
        # Null once the replied-to message is deleted; the reply survives.
        conn.execute(
            "ALTER TABLE messages ADD COLUMN reply_to INTEGER"
            " REFERENCES messages(id) ON DELETE SET NULL"
        )


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


def avatar_url(row):
    """URL of the uploaded profile picture, or None to use the initial."""
    keys = row.keys()
    name = row["avatar"] if "avatar" in keys else None
    return f"/uploads/{name}" if name else None


def public_user(row, online=False):
    return {
        "id": row["id"],
        "username": row["username"],
        "discriminator": row["discriminator"],
        "tag": f'{row["username"]}#{row["discriminator"]}',
        "color": row["color"],
        "bio": row["bio"] if "bio" in row.keys() else "",
        "avatarUrl": avatar_url(row),
        "isBot": bool(row["is_bot"]) if "is_bot" in row.keys() else False,
        "status": row["status"] if "status" in row.keys() else "",
        "online": online,
    }


ONLINE_WINDOW = 70  # seconds since last poll


def is_online(row):
    try:
        return (now() - (row["last_seen"] or 0)) < ONLINE_WINDOW
    except (IndexError, KeyError):
        return False
