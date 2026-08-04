"""SQLite storage layer. Stdlib only."""

import datetime
import os
import re
import secrets
import sqlite3
import hashlib
import time
import zoneinfo

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

-- A blackjack hand posted by the Frontman bot. `state` is the JSON blob from
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

-- Things bought from the Frontman's shop. One row per perk per person; the
-- price paid is kept so a refund or an audit doesn't need the price list.
CREATE TABLE IF NOT EXISTS purchases (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    item       TEXT NOT NULL,
    price      INTEGER NOT NULL,
    created_at INTEGER NOT NULL,
    UNIQUE (user_id, item)
);

-- Lottery tickets. One row per ticket, so buying ten is ten chances.
-- `draw_at` is the moment of the draw the ticket is entered into, so a ticket
-- bought after today's draw goes into tomorrow's.
CREATE TABLE IF NOT EXISTS lottery_tickets (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    draw_at    INTEGER NOT NULL,
    price      INTEGER NOT NULL,
    won        INTEGER NOT NULL DEFAULT 0,
    drawn      INTEGER NOT NULL DEFAULT 0,
    seen       INTEGER NOT NULL DEFAULT 0,
    created_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_lottery_user ON lottery_tickets(user_id, drawn);

-- One row per draw that has actually run. The primary key is what stops a
-- draw happening twice: whoever inserts the row first owns that draw, so a
-- restart, a slow announcement or two threads can't pay the prize out twice.
CREATE TABLE IF NOT EXISTS lottery_draws (
    draw_at   INTEGER PRIMARY KEY,
    ran_at    INTEGER NOT NULL,
    tickets   INTEGER NOT NULL DEFAULT 0,
    winners   INTEGER NOT NULL DEFAULT 0,
    prize     INTEGER NOT NULL DEFAULT 0,
    announced INTEGER NOT NULL DEFAULT 0
);

-- Muted channels. A row means "no badges and no chime from this channel";
-- unread state itself is untouched, so unmuting restores the real counts.
CREATE TABLE IF NOT EXISTS channel_mutes (
    user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    channel_id INTEGER NOT NULL REFERENCES channels(id) ON DELETE CASCADE,
    created_at INTEGER NOT NULL,
    UNIQUE (user_id, channel_id)
);

-- A poll posted with /poll. `options` is a JSON list of labels; votes point at
-- an index into it, so editing is impossible and the labels can't drift.
CREATE TABLE IF NOT EXISTS polls (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    channel_id INTEGER NOT NULL REFERENCES channels(id) ON DELETE CASCADE,
    message_id INTEGER REFERENCES messages(id) ON DELETE CASCADE,
    author_id  INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    question   TEXT NOT NULL,
    options    TEXT NOT NULL,
    multi      INTEGER NOT NULL DEFAULT 0,
    closed     INTEGER NOT NULL DEFAULT 0,
    created_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS poll_votes (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    poll_id    INTEGER NOT NULL REFERENCES polls(id) ON DELETE CASCADE,
    user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    choice     INTEGER NOT NULL,
    created_at INTEGER NOT NULL,
    UNIQUE (poll_id, user_id, choice)
);
CREATE INDEX IF NOT EXISTS idx_poll_votes ON poll_votes(poll_id);

-- Running totals per player for the Frontman leaderboard.
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
DAILY_CLAIM = 5000
CLAIM_INTERVAL = 60 * 60 * 24

# The shift. Frequent enough that someone who busts out is never stuck waiting
# a day to play, and unreliable enough that it isn't a substitute for playing:
# one shift in five goes wrong and pays nothing.
WORK_PAY = 500
WORK_INTERVAL = 60 * 60
WORK_INTERVAL_VAULT = 60 * 30     # halved by the Sana Vault perk
WORK_SUCCESS = 0.8

# Stick at it and you get a rise: 20% more every five hours on the clock,
# compounding, until the pay packet tops out.
WORK_RAISE = 0.2
WORK_RAISE_EVERY = 5
WORK_MAX_PAY = 10_000


def work_rate(shifts):
    """Base pay for the next shift, after every rise earned so far.

    Hours count whether or not the shift went well — a wasted afternoon is
    still an afternoon, and losing the pay is punishment enough.
    """
    rises = shifts // WORK_RAISE_EVERY
    return min(WORK_MAX_PAY, round(WORK_PAY * (1 + WORK_RAISE) ** rises))


def shifts_to_raise(shifts):
    """Hours left before the next rise, or 0 once the pay has topped out."""
    if work_rate(shifts) >= WORK_MAX_PAY:
        return 0
    return WORK_RAISE_EVERY - (shifts % WORK_RAISE_EVERY)

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


# ------------------------------------------------------------- the lottery

LOTTERY_PRICE = 500
LOTTERY_ODDS = 300              # one in this many tickets wins
LOTTERY_PRIZE = 100_000

# Six o'clock in Australian eastern time, not wherever the server happens to
# be — Railway runs in UTC, and the draw should land at the same time on the
# players' clocks whichever machine it runs on.
DRAW_HOUR = max(0, min(23, int(os.environ.get("NEXUS_DRAW_HOUR", "18"))))
DRAW_TZ_NAME = os.environ.get("NEXUS_DRAW_TZ") or "Australia/Sydney"


def _draw_zone():
    """The zone the draw hour is read in.

    Australia/Sydney rather than a fixed +10 so the draw stays at six on the
    wall clock through daylight saving instead of drifting to five. Container
    images sometimes ship without the tz database; a fixed AEST offset is the
    fallback, which is exactly right for Brisbane and an hour out for Sydney
    over summer.
    """
    try:
        return zoneinfo.ZoneInfo(DRAW_TZ_NAME)
    except Exception:
        return datetime.timezone(datetime.timedelta(hours=10), "AEST")


DRAW_ZONE = _draw_zone()


def next_draw_at(ts=None):
    """When the next draw happens: the next DRAW_HOUR at or after `ts`."""
    ts = int(ts if ts is not None else now())
    here = datetime.datetime.fromtimestamp(ts, DRAW_ZONE)
    at = here.replace(hour=DRAW_HOUR, minute=0, second=0, microsecond=0)
    if at.timestamp() <= ts:
        # Adding a day is 24 real hours, which lands an hour off across a
        # daylight-saving change — so the wall clock is pinned again after.
        at = (at + datetime.timedelta(days=1)).replace(
            hour=DRAW_HOUR, minute=0, second=0, microsecond=0)
    return int(at.timestamp())


def draw_clock(ts=None):
    """The draw time as the players read it, for the banner."""
    at = datetime.datetime.fromtimestamp(next_draw_at(ts), DRAW_ZONE)
    return f"{at:%H:%M} {at:%Z}"


def next_draw_in(ts=None):
    ts = int(ts if ts is not None else now())
    return max(0, next_draw_at(ts) - ts)


def buy_ticket(conn, user_id, price=LOTTERY_PRICE):
    conn.execute(
        "INSERT INTO lottery_tickets (user_id, draw_at, price, created_at)"
        " VALUES (?,?,?,?)",
        (user_id, next_draw_at(), price, now()),
    )


def pending_tickets(conn, user_id):
    """This player's tickets waiting on the next draw."""
    return conn.execute(
        "SELECT COUNT(*) c FROM lottery_tickets WHERE user_id = ? AND drawn = 0",
        (user_id,),
    ).fetchone()["c"]


def due_draw(conn):
    """The draw that should have run by now and hasn't, or None."""
    row = conn.execute(
        "SELECT MIN(draw_at) d FROM lottery_tickets WHERE drawn = 0 AND draw_at <= ?",
        (now(),),
    ).fetchone()
    return row["d"]


def run_draw(conn, draw_at):
    """Roll every ticket in one draw and pay the winners.

    Each ticket is rolled on its own, so ten tickets are ten one-in-three-
    hundred chances rather than one better chance. The roll happens here and
    not at the counter, so a ticket can't be inspected before its draw.

    Returns the draw's summary, or None if another caller got there first —
    the primary key on lottery_draws is what makes that race safe.
    """
    try:
        with conn:
            conn.execute(
                "INSERT INTO lottery_draws (draw_at, ran_at) VALUES (?,?)",
                (draw_at, now()),
            )
    except sqlite3.IntegrityError:
        return None                       # somebody else is running this one

    rows = conn.execute(
        "SELECT id, user_id FROM lottery_tickets WHERE drawn = 0 AND draw_at <= ?",
        (draw_at,),
    ).fetchall()

    rng = secrets.SystemRandom()
    winners = {}
    with conn:
        for r in rows:
            won = LOTTERY_PRIZE if rng.randrange(LOTTERY_ODDS) == 0 else 0
            conn.execute(
                "UPDATE lottery_tickets SET drawn = 1, won = ? WHERE id = ?",
                (won, r["id"]),
            )
            if won:
                winners[r["user_id"]] = winners.get(r["user_id"], 0) + won
                adjust_coins(conn, r["user_id"], won)
        conn.execute(
            "UPDATE lottery_draws SET tickets = ?, winners = ?, prize = ?"
            " WHERE draw_at = ?",
            (len(rows), len(winners), sum(winners.values()), draw_at),
        )
    return {
        "drawAt": draw_at,
        "tickets": len(rows),
        "winners": winners,             # {user_id: total won}
    }


def unannounced_draws(conn):
    return conn.execute(
        "SELECT * FROM lottery_draws WHERE announced = 0 ORDER BY draw_at"
    ).fetchall()


def mark_announced(conn, draw_at):
    with conn:
        conn.execute(
            "UPDATE lottery_draws SET announced = 1 WHERE draw_at = ?", (draw_at,)
        )


def draw_winners(conn, draw_at):
    """Who won a given draw, and how much."""
    return conn.execute(
        "SELECT t.user_id, u.username, u.discriminator, SUM(t.won) won,"
        " COUNT(*) tickets FROM lottery_tickets t JOIN users u ON u.id = t.user_id"
        " WHERE t.draw_at = ? AND t.won > 0 GROUP BY t.user_id"
        " ORDER BY won DESC, u.username COLLATE NOCASE",
        (draw_at,),
    ).fetchall()


def draw_entries(conn, draw_at):
    """How many tickets and how many people were in a draw."""
    return conn.execute(
        "SELECT COUNT(*) tickets, COUNT(DISTINCT user_id) players"
        " FROM lottery_tickets WHERE draw_at = ?",
        (draw_at,),
    ).fetchone()


def collect_results(conn, user_id):
    """Settled tickets the owner hasn't been told about yet, marked as told."""
    rows = conn.execute(
        "SELECT id, won FROM lottery_tickets"
        " WHERE user_id = ? AND drawn = 1 AND seen = 0 ORDER BY id",
        (user_id,),
    ).fetchall()
    if not rows:
        return None
    with conn:
        conn.execute(
            "UPDATE lottery_tickets SET seen = 1 WHERE user_id = ? AND drawn = 1",
            (user_id,),
        )
    won = sum(r["won"] for r in rows)
    return {
        "tickets": len(rows),
        "winners": sum(1 for r in rows if r["won"]),
        "won": won,
    }


# ---------------------------------------------------------------- the shop

# Perks are permanent and deliberately expensive: at 5,000 a day from /claim
# and 500 an hour from /work, the cheapest is a fortnight of showing up and the
# Sana Lounge is something you win at the tables rather than wait for.
SHOP_ITEMS = [
    {
        "id": "lottery",
        "name": "Lottery ticket",
        "price": LOTTERY_PRICE,
        "icon": "🎟",
        # The only thing on the shelf you can buy twice, and the only one that
        # might be worth nothing in the morning.
        "repeatable": True,
        "summary": f"One in {LOTTERY_ODDS} wins {LOTTERY_PRIZE:,}.",
        "detail": "Drawn once a day. Buy as many as you like — each one is its "
                  "own chance.",
    },
    {
        "id": "fedora",
        "name": "Fedora",
        "price": 10_000,
        "icon": "🎩",
        # The one perk you can put away: it sits on the avatar rather than
        # changing how a name is drawn, and taste varies.
        "decoration": True,
        "summary": "A hat that sits on top of your avatar.",
        "detail": "Take it off or put it back on any time in Settings.",
    },
    {
        "id": "glow",
        "name": "Glowing nameplate",
        "price": 100_000,
        "icon": "✨",
        "summary": "Your name glows wherever it appears.",
        "detail": "Chat, the member list and every leaderboard.",
    },
    {
        "id": "badge",
        "name": "VIP badge",
        "price": 150_000,
        "icon": "✦",
        "summary": "A VIP tag beside your name.",
        "detail": "Sits where the BOT tag does, in gold.",
    },
    {
        "id": "ring",
        "name": "Prismatic ring",
        "price": 200_000,
        "icon": "◍",
        "summary": "An animated ring around your avatar.",
        "detail": "Shows on your messages, the member list and your profile.",
    },
    {
        "id": "goldpass",
        "name": "Gold Pass",
        "price": 300_000,
        "icon": "🏅",
        "summary": "Double pay from /claim and /work, forever.",
        "detail": f"{DAILY_CLAIM * 2:,} a day, and double whatever your shift "
                  f"is paying — {WORK_MAX_PAY * 2:,} once you're on top rate.",
    },
    {
        "id": "vault",
        "name": "Sana Vault",
        "price": 500_000,
        "icon": "🏦",
        "summary": "Work every 30 minutes instead of every hour.",
        "detail": "Stacks with the Gold Pass.",
    },
    {
        "id": "lounge",
        "name": "Sana Lounge key",
        "price": 1_000_000,
        "icon": "🔑",
        "summary": "Opens the private #sana-lounge channel.",
        "detail": "In every server you are in, now and later. Nobody without a "
                  "key can see it — not even the server owner.",
    },
]

SHOP_BY_ID = {item["id"]: item for item in SHOP_ITEMS}

LOUNGE_CHANNEL = "sana-lounge"


def perks_for(conn, user_id):
    """The set of shop items this player owns."""
    return {
        r["item"]
        for r in conn.execute("SELECT item FROM purchases WHERE user_id = ?", (user_id,))
    }


def perks_map(conn, user_ids):
    """{user_id: {perk, ...}} for a batch of people, in one query."""
    ids = list(user_ids)
    if not ids:
        return {}
    marks = ",".join("?" * len(ids))
    out = {uid: set() for uid in ids}
    for r in conn.execute(
        f"SELECT user_id, item FROM purchases WHERE user_id IN ({marks})", ids
    ):
        out[r["user_id"]].add(r["item"])
    return out


def claim_amount(perks):
    return DAILY_CLAIM * 2 if "goldpass" in perks else DAILY_CLAIM


def work_pay(perks, shifts=0):
    """What the next successful shift pays this person.

    The Gold Pass doubles it on top of the cap: a perk that cost 300,000 has
    to keep meaning something once someone has worked their way to the top.
    """
    rate = work_rate(shifts)
    return rate * 2 if "goldpass" in perks else rate


def work_interval(perks):
    return WORK_INTERVAL_VAULT if "vault" in perks else WORK_INTERVAL


BOT_NAME = "Frontman"
BOT_TAG = "0000"
# The bot's identity in the database, and the reason it still says "gamesman":
# ensure_bot finds the account by this address. Changing it would leave the
# old bot in place with every card it ever posted and stand a second one
# beside it, so the name changes and the address does not.
BOT_EMAIL = "gamesman@nexus.bot"
# The name is drawn in this colour, so it has to read against a dark
# background — the mask's own near-black would be invisible.
BOT_COLOR = "#d6d9e2"
BOT_BIO = "Runs the games. Type / to see what's on."

# The mask, kept in the repo and copied into the uploads directory on boot.
# Uploads are only served under a strict 32-hex name, so its stored name is
# derived from a constant rather than being random like everyone else's.
BOT_AVATAR_SRC = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "static", "frontman.png"
)
BOT_AVATAR_NAME = hashlib.sha256(b"frontman-avatar").hexdigest()[:32] + ".png"


def ensure_bot(conn):
    """Create the Frontman account and put it in every server.

    It owns no password anyone can use — the hash is random bytes and login
    refuses bot accounts outright — but it needs a real users row so its
    messages, avatar and member-list entry work like anyone else's.
    """
    row = conn.execute("SELECT id FROM users WHERE email = ?", (BOT_EMAIL,)).fetchone()
    if row:
        bot_id = row["id"]
        # Renaming the bot has to reach databases that already exist, or an
        # older install keeps whatever it was called when it was created.
        conn.execute(
            "UPDATE users SET username = ?, discriminator = ?, bio = ?, color = ?"
            " WHERE id = ?",
            (BOT_NAME, BOT_TAG, BOT_BIO, BOT_COLOR, bot_id),
        )
    else:
        pw_hash, salt = hash_password(secrets.token_hex(32))
        cur = conn.execute(
            "INSERT INTO users (username, discriminator, email, password_hash, salt,"
            " color, bio, created_at, last_seen, is_bot)"
            " VALUES (?,?,?,?,?,?,?,?,?,1)",
            (BOT_NAME, BOT_TAG, BOT_EMAIL, pw_hash, salt, BOT_COLOR,
             BOT_BIO, now(), now()),
        )
        bot_id = cur.lastrowid

    ensure_bot_avatar(conn, bot_id)

    # Join any server it isn't in yet, including ones made before it existed.
    conn.execute(
        "INSERT OR IGNORE INTO guild_members (guild_id, user_id, joined_at)"
        " SELECT g.id, ?, ? FROM guilds g"
        " WHERE NOT EXISTS (SELECT 1 FROM guild_members m"
        "                   WHERE m.guild_id = g.id AND m.user_id = ?)",
        (bot_id, now(), bot_id),
    )
    return bot_id


def ensure_bot_avatar(conn, uid):
    """Copy the mask into the uploads directory and point the bot at it.

    Re-copied whenever the bytes differ, so replacing static/frontman.png and
    restarting is all it takes to change the bot's picture — on a Railway
    volume the uploads directory outlives the deploy that wrote it.
    """
    try:
        with open(BOT_AVATAR_SRC, "rb") as fh:
            source = fh.read()
    except OSError:
        return                      # no picture in the repo: keep the initial
    target = os.path.join(UPLOAD_DIR, BOT_AVATAR_NAME)
    try:
        with open(target, "rb") as fh:
            current = fh.read()
    except OSError:
        current = None
    if current != source:
        with open(target, "wb") as fh:
            fh.write(source)
    conn.execute("UPDATE users SET avatar = ? WHERE id = ?", (BOT_AVATAR_NAME, uid))


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
    if "last_work" not in have:
        conn.execute("ALTER TABLE users ADD COLUMN last_work INTEGER NOT NULL DEFAULT 0")
    if "decoration" not in have:
        # What they are wearing on their avatar, or '' for nothing. Separate
        # from owning it: the hat comes off in settings and goes back on
        # without buying it again.
        conn.execute("ALTER TABLE users ADD COLUMN decoration TEXT NOT NULL DEFAULT ''")
    if "work_shifts" not in have:
        # Hours on the clock, which is what earns the 20% rises.
        conn.execute("ALTER TABLE users ADD COLUMN work_shifts INTEGER NOT NULL DEFAULT 0")

    have = {r["name"] for r in conn.execute("PRAGMA table_info(channels)")}
    if "locked" not in have:
        # 0 for an ordinary channel; otherwise the shop perk that unlocks it.
        conn.execute("ALTER TABLE channels ADD COLUMN locked TEXT NOT NULL DEFAULT ''")
    if "position" not in have:
        conn.execute("ALTER TABLE channels ADD COLUMN position INTEGER NOT NULL DEFAULT 0")
        # Seed the order from the existing ids so nothing appears to move.
        conn.execute(
            "UPDATE channels SET position = ("
            "  SELECT COUNT(*) FROM channels c2"
            "  WHERE c2.guild_id = channels.guild_id AND c2.id < channels.id)"
        )

    have = {r["name"] for r in conn.execute("PRAGMA table_info(guilds)")}
    if "icon" not in have:
        # Stored upload name, or NULL to fall back to the coloured initials.
        conn.execute("ALTER TABLE guilds ADD COLUMN icon TEXT")

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

    if "poll_id" not in have:
        # A poll message carries no text of its own; the card is the message.
        conn.execute(
            "ALTER TABLE messages ADD COLUMN poll_id INTEGER"
            " REFERENCES polls(id) ON DELETE CASCADE"
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


def decoration_of(row):
    keys = row.keys()
    return row["decoration"] if "decoration" in keys and row["decoration"] else ""


def public_user(row, online=False, perks=()):
    return {
        "id": row["id"],
        "perks": sorted(perks),
        "decoration": decoration_of(row),
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
