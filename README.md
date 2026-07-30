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

Uploads default to an `uploads/` folder beside the database, so setting
`NEXUS_DB=/data/nexus.db` puts images on the same Railway volume automatically —
there is no second variable to set.

---

## What's in the app

**Accounts** — email + password sign-up. Passwords are stored as PBKDF2-SHA256
with 200,000 iterations and a per-user salt. Sessions are HttpOnly cookies with
a sliding 30-day expiry.

**Servers and channels** — anyone can create a server; it starts with `#general`
and `#random`. The owner can add and delete channels, rename the server, remove
members, and delete messages. Members can leave.

**Invite codes** — 8-character codes, optionally limited by number of uses or an
expiry. Share the bare code or a full `/invite/<code>` link. Codes can be
revoked by the owner.

**Friends** — added by full tag (`Alex#0421`). Requests can be accepted, ignored
or cancelled; if two people request each other, it auto-accepts.

**Direct messages** — one-to-one conversations with unread badges.

**Images** — drag and drop onto the chat, paste from the clipboard, or use the
`+` button. PNG, JPEG, GIF and WebP up to 8 MB. Click one to open it full size.

**Reactions** — hover a message and pick an emoji, or click an existing pill to
join in. Clicking your own reaction again removes it.

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
| `images.py`        | Image sniffing and size checks for uploads     |
| `static/index.html`| Page shell                                     |
| `static/app.js`    | Entire client — vanilla JS, no build step      |
| `static/style.css` | Styles                                         |
| `railway.json`     | Railway build/deploy config                    |
| `Procfile`         | Start command for Heroku-style platforms       |
