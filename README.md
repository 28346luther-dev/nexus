# Nexus

A Discord-style messaging site: accounts, servers with text channels, invite
codes, friend requests and direct messages.

Built on the Python standard library and SQLite — **no dependencies to install**.

---

## Run it locally

```bash
python3 app.py
```

Then open <http://localhost:8000>.

The startup banner prints two addresses. The second one (your Wi-Fi address,
like `http://192.168.1.225:8000`) is the one to give other people so they can
create accounts and use your invite codes. `localhost` only ever means "this
computer", so an invite link containing it will not work for anyone else.

To keep the server private to your own machine:

```bash
python3 app.py --host 127.0.0.1
```

---

## Deploy to Railway

The repo is ready to deploy as-is. Everything below is Railway's own UI — no
code changes needed.

### 1. Push the code to GitHub

```bash
git init
git add .
git commit -m "Nexus messaging app"
git branch -M main
git remote add origin https://github.com/<you>/<repo>.git
git push -u origin main
```

`.gitignore` already excludes `data.db`, so local accounts and messages stay off
GitHub.

### 2. Create the Railway project

In Railway: **New Project → Deploy from GitHub repo**, and pick the repo.
Railway reads `railway.json` and `requirements.txt`, detects Python, and starts
it with `python3 app.py`. It injects `PORT`; the app binds to it automatically.

### 3. Add a Volume — do this before inviting anyone

Railway containers get a fresh filesystem on every deploy. Without a volume,
**every account and message is wiped each time you push**.

- Service → **Variables** → add:

  | Variable   | Value             |
  |------------|-------------------|
  | `NEXUS_DB` | `/data/nexus.db`  |

- Service → **Settings → Volumes** → **Add Volume**, mount path `/data`.

The app creates the directory and database file on first boot.

### 4. Generate a public domain

Service → **Settings → Networking → Generate Domain**.

That gives you something like `https://nexus-production.up.railway.app`. Share
that URL — people create their own accounts from the sign-up screen.

Invite links generated inside the app automatically use this domain (the app
reads the `X-Forwarded-Host` / `X-Forwarded-Proto` headers from Railway's
proxy), and session cookies are marked `Secure` because Railway serves HTTPS.

### Shipping updates without losing data

Pushing to `main` redeploys. User data is **not** touched by a deploy, because
it lives on the volume rather than in the container:

- Accounts, servers, messages, reactions and stickers are rows in
  `/data/nexus.db`.
- Images sit in `/data/uploads/`.
- Schema changes are additive — new tables use `CREATE TABLE IF NOT EXISTS`
  and new columns are added with `ALTER TABLE ... ADD COLUMN` in `migrate()`.
  Nothing drops or recreates an existing table.
- Sessions survive too, so nobody gets logged out mid-conversation.

Every boot prints what it found. A healthy update logs:

```
Existing data found.
Storage: persistent volume at /data — data carries across deploys.
```

If a deploy ever logs `*** WARNING: no volume is attached. ***` or
`Started a new, empty database.` on a site that already had users, stop and fix
the volume before anyone signs up again — that deploy is running on throwaway
storage.

**What would actually destroy data:** detaching or deleting the volume,
changing `NEXUS_DB` to a different path, deleting the Railway service, or a
future migration that drops or rebuilds a table. Adding columns and tables is
safe; renaming or removing them is not.

**Back up before anything risky.** The database is one file, so:

```
railway run cat /data/nexus.db > backup-$(date +%F).db
```

### Health check

`GET /api/health` returns `{"ok": true}`; `railway.json` already points the
platform's health check at it.

---

## Environment variables

| Variable        | Default              | Purpose                                          |
|-----------------|----------------------|--------------------------------------------------|
| `PORT`          | `8000`               | Port to listen on. Railway sets this for you.     |
| `HOST`          | `0.0.0.0`            | Interface to bind.                                |
| `NEXUS_DB`      | `./data.db`          | Database location. Point at your volume in prod.  |
| `NEXUS_UPLOADS` | next to `NEXUS_DB`   | Where uploaded images are written.                |
| `GIPHY_API_KEY` | unset                | Enables `/gif`. Without it the picker says so.    |

### Turning on GIFs

Get a free key at [developers.giphy.com](https://developers.giphy.com), then in
Railway add a variable:

| Variable        | Value            |
|-----------------|------------------|
| `GIPHY_API_KEY` | your key         |

The key stays on the server — the browser only ever talks to `/api/gifs`, which
proxies Giphy and passes back just the fields the picker needs. Never put it in
the repo or in client code.

To use `/gif` locally, export it first:

```bash
export GIPHY_API_KEY=your-key-here
```

**macOS note:** python.org builds ship without a CA bundle, so HTTPS calls to
Giphy fail with a certificate error. Run the `Install Certificates.command`
inside `/Applications/Python 3.x/`, or start the server with
`SSL_CERT_FILE=/etc/ssl/cert.pem`. Linux hosts like Railway are unaffected.

Uploads default to an `uploads/` folder beside the database, so setting
`NEXUS_DB=/data/nexus.db` puts images on the same Railway volume automatically —
there is no second variable to set.

---

## What's in the app

**Accounts** — email + password sign-up. Passwords are stored as PBKDF2-SHA256
with 200,000 iterations and a per-user salt. Sessions are HttpOnly cookies with
a sliding 30-day expiry.

**Servers and channels** — anyone can create a server; it starts with `#general`
and `#random`. The owner can add and delete channels, **drag channels in the
sidebar to reorder them**, set a **server icon** (*Server options → Change
server icon*, PNG/JPEG/GIF/WebP up to 2 MB), rename the server, remove members,
and delete messages. Members can leave. New channels are added at the bottom of
the list, and the order is shared with everyone in the server.

**Invite codes** — 8-character codes, optionally limited by number of uses or an
expiry. Share the bare code or a full `/invite/<code>` link. Codes can be
revoked by the owner.

**Friends** — added by full tag (`Alex#0421`). Requests can be accepted, ignored
or cancelled; if two people request each other, it auto-accepts.

**Direct messages** — one-to-one conversations with unread badges.

**Frontman (games and Sana Coin)** — a masked bot account that joins every
server automatically, including ones created before it existed. Everything it
does is driven by slash commands and answered as a card in the channel. Type
`/` to see the list, arrows to choose, Enter to run.

Its picture is `static/frontman.png`, copied into the uploads directory on
boot. Replace that file and restart to change the mask; the bot is found by a
fixed address in the database, so the account, its name and every card it has
ever posted survive the change.

| Command | What it does |
|---|---|
| `/blackjack cpu [bet]` | Against the dealer, which hits to 17. Blackjack pays 3:2 |
| `/blackjack 1v1 [bet]` | Open a table; anyone in the server can **Join** by matching the stake |
| `/poker cpu [bet]` | Hold'em against three house players |
| `/poker table [bet]` | Open a Hold'em table for up to 8 people |
| `/roulette [bet]` | Single-zero wheel: colours, odds/evens, dozens, columns or a straight number |
| `/slots <bet>` | Three weighted reels |
| `/claim` | Collect 5,000 Sana Coin, once every 24 hours |
| `/work` | Do a shift for Sana Coin, once an hour |
| `/shop` | Spend Sana Coin on permanent perks |
| `/balance` | Your coins, rank and record |
| `/bank` | Everyone's balances in this server |
| `/leaderboard` | Top players by games won |
| `/poll <question>` | Put a question to the channel and count the votes |
| `/reset` | Wipe balances and records back to the start — server owner only |

Leave the bet off and you get a **"How much would you like to bet?"** dialog
with quick amounts, All in, and a For fun option.

**Sana Coin** — everyone starts with 2,000. Stakes are held out of your balance
when a hand starts and paid out exactly once. Ranks (Rookie → Chancer →
Hustler → Sharp → High Roller → Sana Legend) come from **wins, not balance**,
so they can't simply be bought.

### Working for it

`/work` is an hour's shift for **2,000 Sana Coin** — four times out of five.
The fifth is a bad day: you reversed the drinks trolley over the boss's cat, or
let a man in a false moustache walk out with the chip tray, and you go home
unpaid. The hour still counts.

Every **five hours on the clock** earns a **20% rise**, compounding, until the
pay packet tops out at **10,000** a shift — about 45 hours of work. The Gold
Pass doubles whatever you're on, cap included, and the Sana Vault halves the
wait between shifts.

**Card secrecy.** In 1v1 blackjack you see only your own cards until the hand
settles, and spectators see neither — showing both would let whoever acts
second read the total they need. Poker hole cards work the same way. The
dealer's hole card in the CPU game stays down until you stand.

### Hold'em

Betting works the way it does at a real table. Everyone antes to sit down.
Each street then opens with nothing to call, so **Check** is free; **Raise**
puts money in and sets a price everyone else has to answer, which reopens the
turn of anyone who had already acted; **Call** matches what's owed and **Fold**
gets out. Raises go in whole antes, up to twenty at a time. Best five cards of
your seven wins, and ties split the pot.

### Blackjack

**Double** takes one more card for a second stake and ends the hand.
**Split** turns a pair into two hands, each drawing a second card and each
carrying its own stake — you can split up to four hands and double any of
them. Split aces get one card each and stand, and a 21 made from a split pays
even money rather than 3:2, exactly as at a real table. Both are for hands
against the dealer: a 1v1 is a matched stake on both sides, and there is no way
to match half a split.

### Roulette

37 pockets, single zero. Red/black, odd/even and 1–18/19–36 pay 1 to 1;
dozens and columns pay 2 to 1; a straight number pays 35 to 1. Zero loses every
outside bet, which is where the house edge lives.

### The shop

`/shop` sells six things, permanently and without refunds. Prices are steep on
purpose — the cheapest is a fortnight of showing up, and the Sana Lounge is
something you win at the tables.

| Perk | Price | What it does |
|---|---|---|
| Glowing nameplate | 100,000 | Your name glows in chat, the member list and every leaderboard |
| VIP badge | 150,000 | A gold VIP tag beside your name |
| Prismatic ring | 200,000 | An animated ring around your avatar |
| Gold Pass | 300,000 | `/claim` and `/work` pay double, forever |
| Sana Vault | 500,000 | `/work` every 30 minutes instead of every hour |
| Sana Lounge key | 1,000,000 | Opens the private `#sana-lounge` channel |

The **Sana Lounge** appears in every server you are in, now and later, and only
for people who hold a key — **not even the server owner can see it** without
one, and it can't be deleted or dragged around. `/reset` levels balances and
records but leaves perks alone: they were paid for.

**GIFs** — the **GIF** button in the message bar, or `/gif <search>`, opens a
Giphy picker. Tap ★ on any GIF to keep it,
and the **Favourites** tab holds up to 100. Requires `GIPHY_API_KEY` (below).

**Replies** — hover a message and hit ↩. The composer shows who you're
answering (Escape cancels), and the sent reply carries a preview of the
original — click it to jump there and the message flashes. Deleting the
original leaves the reply intact, marked "Original message was deleted".

**Mentions** — type `@` in the composer to get a member list; arrows or Tab and
Enter to pick, Escape to dismiss. Mentions are stored as the full tag
(`@Alex#0421`) so two people sharing a username never get confused, and render
as a plain `@Alex` pill. `@everyone` pings the whole server. A message that
mentions you gets a gold edge, and channels with unread pings show an `@` badge
that outranks the normal unread count.

**Images** — drag and drop onto the chat, paste from the clipboard, or use the
`+` button. PNG, JPEG, GIF and WebP up to 8 MB. Click one to open it full size.

**Reactions** — hover a message and pick an emoji, or click an existing pill to
join in. Clicking your own reaction again removes it. Hovering a pill shows
**who reacted**, with their avatars; the list is refetched each time rather than
cached, because people add and remove reactions while you're reading.

**Polls** — `/poll` opens a builder: a question, two to six options, and
optionally more than one answer each. Votes land live for everyone, the leading
option is highlighted once there is a clear one, and clicking your own choice
again takes the vote back. Whoever started the poll (or the server owner) can
close it, which freezes the result on show.

**Muting channels** — hover a channel in the sidebar and hit the bell. A muted
channel dims, stops showing unread and mention badges, stops counting towards
the server's badge in the rail, and never chimes. Unread state itself is
untouched, so unmuting brings back the count that was there all along. Mutes
are per person.

**Custom stickers** — each server has its own set, managed by the owner under
*Server options → Manage stickers*. Up to 50 per server, 1 MB each. Removing a
sticker keeps it visible in messages that already used it, and frees the name.

**Profile pictures** — upload one in *Settings* (⚙ next to your name), up to
2 MB, or remove it to go back to the coloured initial.

**Live updates** — a long-polling endpoint (`/api/poll`) pushes new messages
within a few hundred milliseconds and drives unread badges and online status.
Reactions and edits are picked up through a per-channel fingerprint, since they
don't create new messages.

### A note on uploaded files

Uploads are validated by inspecting the actual bytes, not the filename or the
declared type, and only PNG/JPEG/GIF/WebP are accepted — SVG is rejected
deliberately, because it can carry scripts that would run on your domain. Files
are stored under random names, served with `nosniff` and a locked content type,
and can never be interpreted as HTML.

---

## Scale expectations

`ThreadingHTTPServer` handles one thread per connection, and each open tab holds
a long poll for up to 20 seconds at a time. That is comfortable for a group of
friends or a class — think tens of concurrent users on Railway's starter plan,
not thousands. SQLite in WAL mode is the storage engine, so it wants a single
instance: don't scale the service past one replica.

If you outgrow that, the natural upgrades are swapping the long poll for
WebSockets and moving from SQLite to Postgres.

---

## Files

| Path               | Purpose                                        |
|--------------------|------------------------------------------------|
| `app.py`           | HTTP server, routing, all API endpoints        |
| `db.py`            | Schema, migrations, password hashing           |
| `blackjack.py`     | Card and hand rules, including splits and doubles |
| `poker.py`         | Hold'em rules, betting and hand ranking        |
| `roulette.py`      | The wheel, the bet types and their odds        |
| `slots.py`         | Slot reels and payout table                    |
| `tests/`           | Plain-Python API tests; see tests/README.md    |
| `images.py`        | Image sniffing and size checks for uploads     |
| `static/index.html`| Page shell                                     |
| `static/app.js`    | Entire client — vanilla JS, no build step      |
| `static/style.css` | Styles                                         |
| `railway.json`     | Railway build/deploy config                    |
| `Procfile`         | Start command for Heroku-style platforms       |
