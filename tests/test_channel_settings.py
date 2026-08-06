"""Per-channel rules: slow mode, polls only, no bots."""
import json
import sqlite3
import sys
import time
import urllib.error
import urllib.request

BASE = "http://127.0.0.1:" + (sys.argv[1] if len(sys.argv) > 1 else "8130")
DB_PATH = sys.argv[2] if len(sys.argv) > 2 else "/tmp/nexus_test.db"
PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f'{"PASS" if cond else "FAIL"}  {name}' + (f"   {detail}" if detail and not cond else ""))


class C:
    def __init__(self):
        self.cookie = None
        self.uid = None

    def call(self, method, path, body=None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(BASE + path, data=data, method=method)
        if data:
            req.add_header("Content-Type", "application/json")
        if self.cookie:
            req.add_header("Cookie", self.cookie)
        try:
            with urllib.request.urlopen(req) as res:
                sc = res.headers.get("Set-Cookie")
                if sc:
                    self.cookie = sc.split(";")[0]
                return res.status, json.loads(res.read() or b"{}")
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")


def signup(name, email):
    c = C()
    _, r = c.call("POST", "/api/register", {
        "username": name, "email": email, "password": "hunter2hunter2",
        "fullName": f"{name} Tester"})
    c.uid = r["user"]["id"]
    return c


owner = signup("Keeper", "cs1@x.com")
member = signup("Regular", "cs2@x.com")

_, g = owner.call("POST", "/api/guilds", {"name": "Rules Room"})
gid, cid = g["guildId"], g["channelId"]
_, inv = owner.call("POST", f"/api/guilds/{gid}/invites", {})
member.call("POST", f"/api/invites/{inv['code']}/join")


def channel_info(client, channel_id):
    _, r = client.call("GET", f"/api/channels/{channel_id}")
    return r["channel"]


def back_date_games(client, seconds):
    """Age someone's Frontman use so its cooldown has run out."""
    conn = sqlite3.connect(DB_PATH)
    conn.execute("UPDATE games SET created_at = created_at - ? WHERE host_id = ?",
                 (seconds, client.uid))
    conn.commit()
    conn.close()


def back_date(client, channel_id, seconds):
    """Age someone's last message so a cooldown has run out."""
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "UPDATE messages SET created_at = created_at - ?"
        " WHERE channel_id = ? AND author_id = ?",
        (seconds, channel_id, client.uid),
    )
    conn.commit()
    conn.close()


# ------------------------------------------------------------- the defaults
info = channel_info(owner, cid)
check("slow mode starts off", info["slowMode"] == 0, info)
check("polls only starts off", info["pollsOnly"] is False, info)
check("no bots starts off", info["noBots"] is False, info)

s, r = member.call("PATCH", f"/api/channels/{cid}/settings", {"slowMode": 30})
check("a member can't set the rules", s == 403, (s, r))
s, r = owner.call("PATCH", f"/api/channels/{cid}/settings", {"slowMode": 99999})
check("slow mode has a ceiling", s == 400, (s, r))
s, r = owner.call("PATCH", f"/api/channels/{cid}/settings", {"slowMode": -5})
check("and no negative", s == 400, (s, r))

# -------------------------------------------------------------- slow mode
s, r = owner.call("PATCH", f"/api/channels/{cid}/settings", {"slowMode": 30})
check("the owner sets slow mode", s == 200 and r["channel"]["slowMode"] == 30, r)
check("everyone can see it is on", channel_info(member, cid)["slowMode"] == 30)

s, r = member.call("POST", f"/api/channels/{cid}/messages", {"content": "first"})
check("the first message goes through", s == 200, (s, r))
s, r = member.call("POST", f"/api/channels/{cid}/messages", {"content": "second"})
check("the second is held back", s == 429, (s, r))
check("and says how long is left", "slow mode" in (r.get("error") or "").lower(), r)

s, r = member.call("POST", f"/api/channels/{cid}/polls",
                   {"question": "Now?", "options": ["Yes", "No"]})
check("slow mode paces polls too", s == 429, (s, r))

s, r = owner.call("POST", f"/api/channels/{cid}/messages", {"content": "mine"})
check("the owner is exempt", s == 200, (s, r))
s, r = owner.call("POST", f"/api/channels/{cid}/messages", {"content": "and again"})
check("however fast they go", s == 200, (s, r))

back_date(member, cid, 60)
s, r = member.call("POST", f"/api/channels/{cid}/messages", {"content": "later"})
check("once the wait is over they can post again", s == 200, (s, r))

owner.call("PATCH", f"/api/channels/{cid}/settings", {"slowMode": 0})
s, r = member.call("POST", f"/api/channels/{cid}/messages", {"content": "free"})
check("turning it off lifts it immediately", s == 200, (s, r))

# ------------------------------------------------------------- polls only
s, r = owner.call("PATCH", f"/api/channels/{cid}/settings", {"pollsOnly": True})
check("polls only can be turned on", r["channel"]["pollsOnly"] is True, r)

s, r = member.call("POST", f"/api/channels/{cid}/messages", {"content": "chat"})
check("ordinary messages are refused", s == 403, (s, r))
s, r = owner.call("POST", f"/api/channels/{cid}/messages", {"content": "even mine"})
check("the owner is not exempt from it", s == 403, (s, r))

s, r = member.call("POST", f"/api/channels/{cid}/polls",
                   {"question": "Lunch?", "options": ["Pizza", "Curry"]})
check("but a poll is fine", s == 200, (s, r))

owner.call("PATCH", f"/api/channels/{cid}/settings", {"pollsOnly": False})
s, r = member.call("POST", f"/api/channels/{cid}/messages", {"content": "talking again"})
check("turning it off restores normal chat", s == 200, (s, r))

# ---------------------------------------------------------------- no bots
s, r = owner.call("PATCH", f"/api/channels/{cid}/settings", {"noBots": True})
check("no bots can be turned on", r["channel"]["noBots"] is True, r)

_, before = member.call("GET", "/api/wallet")
s, r = member.call("POST", f"/api/channels/{cid}/games", {"mode": "cpu", "bet": 100})
check("blackjack is turned away", s == 403, (s, r))
s, r = member.call("POST", f"/api/channels/{cid}/poker", {"bet": 100})
check("so is poker", s == 403, (s, r))
s, r = member.call("POST", f"/api/channels/{cid}/slots", {"bet": 100})
check("so are the slots", s == 403, (s, r))
s, r = member.call("POST", f"/api/channels/{cid}/roulette", {"kind": "red", "bet": 100})
check("so is roulette", s == 403, (s, r))
s, r = member.call("POST", f"/api/channels/{cid}/beg")
check("so is begging", s == 403, (s, r))
s, r = member.call("POST", f"/api/channels/{cid}/frontman", {"kind": "balance"})
check("and so are the Frontman's cards", s == 403, (s, r))

_, after = member.call("GET", "/api/wallet")
check("a refused game costs nothing", after["coins"] == before["coins"],
      (before["coins"], after["coins"]))

s, r = member.call("POST", f"/api/channels/{cid}/messages", {"content": "people still talk"})
check("people can still talk", s == 200, (s, r))

owner.call("PATCH", f"/api/channels/{cid}/settings", {"noBots": False})
s, r = member.call("POST", f"/api/channels/{cid}/slots", {"bet": 10})
check("turning it off lets the games back in", s == 200, (s, r))

# ------------------------------------------------------------- all at once
_, r = owner.call("POST", f"/api/guilds/{gid}/channels", {"name": "votes"})
votes = r["channelId"]
s, r = owner.call("PATCH", f"/api/channels/{votes}/settings",
                  {"slowMode": 60, "pollsOnly": True, "noBots": True})
check("all three can be set together",
      r["channel"]["slowMode"] == 60 and r["channel"]["pollsOnly"]
      and r["channel"]["noBots"], r)

_, guilds = member.call("GET", "/api/guilds")
listed = [c for g2 in guilds["guilds"] if g2["id"] == gid
          for c in g2["channels"] if c["id"] == votes][0]
check("the sidebar carries the rules too",
      listed["slowMode"] == 60 and listed["pollsOnly"] and listed["noBots"], listed)

# The rules are per channel, not per server.
first = channel_info(member, cid)
check("another channel is unaffected",
      first["slowMode"] == 0 and not first["pollsOnly"] and not first["noBots"], first)

# --------------------------------------------------- frontman usage limit
_, guilds = owner.call("GET", "/api/guilds")
mine = [g2 for g2 in guilds["guilds"] if g2["id"] == gid][0]
check("the Frontman starts unlimited", mine["frontmanCooldown"] == 0, mine)

s, r = member.call("PATCH", f"/api/guilds/{gid}/settings", {"frontmanCooldown": 60})
check("a member can't set the limit", s == 403, (s, r))
s, r = owner.call("PATCH", f"/api/guilds/{gid}/settings", {"frontmanCooldown": 99})
check("only the slider's values are taken", s == 400, (s, r))

s, r = owner.call("PATCH", f"/api/guilds/{gid}/settings", {"frontmanCooldown": 600})
check("the owner sets a limit",
      s == 200 and r["guild"]["frontmanCooldown"] == 600, r)

# They have already used it earlier in this file, so clear that first.
back_date_games(member, 3600)
s, r = member.call("POST", f"/api/channels/{cid}/slots", {"bet": 10})
check("the first go is allowed", s == 200, (s, r))
s, r = member.call("POST", f"/api/channels/{cid}/slots", {"bet": 10})
check("the second is held back", s == 429, (s, r))
s, r = member.call("POST", f"/api/channels/{cid}/games", {"mode": "cpu", "bet": 10})
check("the limit covers every game, not just the one used", s == 429, (s, r))
s, r = member.call("POST", f"/api/channels/{votes}/frontman", {"kind": "balance"})
check("and every channel in the server", s in (403, 429), (s, r))

s, r = owner.call("POST", f"/api/channels/{cid}/slots", {"bet": 10})
check("the owner is exempt", s == 200, (s, r))
s, r = owner.call("POST", f"/api/channels/{cid}/slots", {"bet": 10})
check("however often they play", s == 200, (s, r))

# Off duty stops it dead, owner included.
s, r = owner.call("PATCH", f"/api/guilds/{gid}/settings", {"frontmanCooldown": -1})
check("the Frontman can be sent off duty",
      r["guild"]["frontmanCooldown"] == -1, r)
s, r = member.call("POST", f"/api/channels/{cid}/slots", {"bet": 10})
check("nobody can call on it then", s == 403, (s, r))
check("and is told why", "off duty" in (r.get("error") or "").lower(), r)
s, r = owner.call("POST", f"/api/channels/{cid}/slots", {"bet": 10})
check("not even the owner", s == 403, (s, r))

s, r = owner.call("PATCH", f"/api/guilds/{gid}/settings", {"frontmanCooldown": 0})
check("and it comes straight back on",
      owner.call("POST", f"/api/channels/{cid}/slots", {"bet": 10})[0] == 200)

print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
if FAIL:
    print("FAILED: " + ", ".join(FAIL))
    sys.exit(1)
