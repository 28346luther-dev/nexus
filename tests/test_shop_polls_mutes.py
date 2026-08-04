"""The shop and its perks, /reset, muted channels and polls."""
import json
import sys
import urllib.error
import urllib.request

BASE = "http://127.0.0.1:" + (sys.argv[1] if len(sys.argv) > 1 else "8130")
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
    _, r = c.call("POST", "/api/register",
                  {"username": name, "email": email, "password": "hunter2hunter2"})
    c.uid = r["user"]["id"]
    return c


owner = signup("Landlord", "shop1@x.com")
member = signup("Punter", "shop2@x.com")

_, g = owner.call("POST", "/api/guilds", {"name": "The Floor"})
gid, cid = g["guildId"], g["channelId"]
_, inv = owner.call("POST", f"/api/guilds/{gid}/invites", {})
member.call("POST", f"/api/invites/{inv['code']}/join")


def channels_of(client, guild_id):
    _, r = client.call("GET", "/api/guilds")
    for guild in r["guilds"]:
        if guild["id"] == guild_id:
            return guild["channels"]
    return []


def stack_the_deck(client, coins):
    """Sitting on a lot of Sana Coin without playing 200 hands for it."""
    import sqlite3
    conn = sqlite3.connect(DB_PATH)
    conn.execute("UPDATE users SET coins = ? WHERE id = ?", (coins, client.uid))
    conn.commit()
    conn.close()


DB_PATH = sys.argv[2] if len(sys.argv) > 2 else "/tmp/nexus_test.db"

# ==================================================================== work
s, r = owner.call("POST", "/api/wallet/work")
check("a shift is worked", s == 200, r)
check("a good shift pays 500, a bad one pays nothing",
      r["earned"] == (500 if r["ok"] else 0), r)
check("the shift comes with a story", bool(r["shift"]), r)
check("the hour goes on the clock either way", r["workShifts"] == 1, r)
check("four shifts to the first rise", r["shiftsToRaise"] == 4, r)
s, r = owner.call("POST", "/api/wallet/work")
check("the shift has a cooldown", s == 429, (s, r))

s, r = owner.call("POST", "/api/wallet/claim")
check("the daily claim is 5,000", s == 200 and r["claimed"] == 5000, r)

# ==================================================================== shop
s, r = member.call("GET", "/api/shop")
check("the shop lists its stock", s == 200 and len(r["items"]) >= 6, r)
prices = {i["id"]: i["price"] for i in r["items"]}
check("the glowing nameplate is 100,000", prices.get("glow") == 100_000, prices)
check("the Sana Lounge is 1,000,000", prices.get("lounge") == 1_000_000, prices)
check("nothing is owned to begin with",
      all(not i["owned"] for i in r["items"]), r["items"])

s, r = member.call("POST", "/api/shop/buy", {"item": "glow"})
check("you can't buy what you can't afford", s == 400, (s, r))
s, r = member.call("POST", "/api/shop/buy", {"item": "unicorn"})
check("the shop refuses to invent stock", s == 404, (s, r))

stack_the_deck(member, 2_000_000)
s, r = member.call("POST", "/api/shop/buy", {"item": "glow"})
check("a perk can be bought", s == 200, r)
check("the price is taken", r["wallet"]["coins"] == 1_900_000, r["wallet"])
check("the wallet lists the perk", r["wallet"]["perks"] == ["glow"], r["wallet"])
s, r = member.call("POST", "/api/shop/buy", {"item": "glow"})
check("a perk is only sold once", s == 409, (s, r))

s, me = member.call("GET", "/api/me")
check("the account carries its perks", me["user"]["perks"] == ["glow"], me)

# The perk travels with the name, so other people can see it.
member.call("POST", f"/api/channels/{cid}/messages", {"content": "evening all"})
_, r = owner.call("GET", f"/api/channels/{cid}/messages")
mine = [m for m in r["messages"] if m["author"]["id"] == member.uid][-1]
check("everyone sees who is glowing", mine["author"]["perks"] == ["glow"], mine["author"])

# --------------------------------------------------------------- the lounge
before = [c["name"] for c in channels_of(member, gid)]
check("no lounge before the key is bought", "sana-lounge" not in before, before)

s, r = member.call("POST", "/api/shop/buy", {"item": "lounge"})
check("the lounge key can be bought", s == 200, r)
after = channels_of(member, gid)
lounge = next((c for c in after if c["name"] == "sana-lounge"), None)
check("the lounge appears for the key holder", lounge is not None, after)
check("the lounge is marked locked", lounge and lounge["locked"] == "lounge", lounge)

owner_sees = [c["name"] for c in channels_of(owner, gid)]
check("the owner cannot see the lounge without a key",
      "sana-lounge" not in owner_sees, owner_sees)

s, r = owner.call("GET", f"/api/channels/{lounge['id']}/messages")
check("the owner is locked out of the lounge", s == 403, (s, r))
s, r = owner.call("POST", f"/api/channels/{lounge['id']}/messages",
                  {"content": "let me in"})
check("the owner cannot post in the lounge", s == 403, (s, r))
s, r = member.call("POST", f"/api/channels/{lounge['id']}/messages",
                   {"content": "quiet in here"})
check("the key holder can post in the lounge", s == 200, r)

s, r = owner.call("DELETE", f"/api/channels/{lounge['id']}")
check("the lounge cannot be deleted", s == 400, (s, r))

# Reordering still works for the owner, who cannot see the locked channel.
visible = [c["id"] for c in channels_of(owner, gid)]
s, r = owner.call("PATCH", f"/api/guilds/{gid}/channels/order",
                  {"order": list(reversed(visible))})
check("channels still reorder around a locked one", s == 200, (s, r))
owner.call("PATCH", f"/api/guilds/{gid}/channels/order", {"order": visible})

# A new server gets a lounge too, because the key is not per-server.
_, g2 = member.call("POST", "/api/guilds", {"name": "Second Room"})
names = [c["name"] for c in channels_of(member, g2["guildId"])]
check("a key opens a lounge in new servers too", "sana-lounge" in names, names)

# ------------------------------------------------------------- gold pass
s, r = member.call("POST", "/api/shop/buy", {"item": "goldpass"})
check("the gold pass can be bought", s == 200, r)
check("the gold pass doubles the daily claim",
      r["wallet"]["dailyAmount"] == 10_000, r["wallet"])
check("the gold pass doubles the shift", r["wallet"]["workPay"] == 1000, r["wallet"])

# =================================================================== mutes
chans = channels_of(member, gid)
target = next(c for c in chans if c["name"] == "general")
check("channels start unmuted", target["muted"] is False, target)

s, r = member.call("POST", f"/api/channels/{target['id']}/mute", {})
check("a channel can be muted", s == 200 and r["muted"] is True, r)
muted = next(c for c in channels_of(member, gid) if c["id"] == target["id"])
check("the mute is remembered", muted["muted"] is True, muted)

other = next(c for c in channels_of(owner, gid) if c["id"] == target["id"])
check("a mute is only yours", other["muted"] is False, other)

s, r = member.call("POST", f"/api/channels/{target['id']}/mute", {})
check("muting again unmutes", r["muted"] is False, r)
s, r = member.call("POST", f"/api/channels/{target['id']}/mute", {"muted": True})
check("mute state can be set outright", r["muted"] is True, r)

outsider = signup("Nobody", "shop3@x.com")
s, r = outsider.call("POST", f"/api/channels/{target['id']}/mute", {})
check("you can't mute a channel you can't see", s == 403, (s, r))

# =================================================================== polls
s, r = owner.call("POST", f"/api/channels/{cid}/polls",
                  {"question": "Lunch?", "options": ["Pizza"]})
check("a poll needs two options", s == 400, (s, r))
s, r = owner.call("POST", f"/api/channels/{cid}/polls",
                  {"question": "Lunch?", "options": ["Pizza", "Pizza"]})
check("duplicate options don't count twice", s == 400, (s, r))

s, r = owner.call("POST", f"/api/channels/{cid}/polls",
                  {"question": "Where are we ordering from?",
                   "options": ["Pizza", "Curry", "Chips"]})
check("a poll posts", s == 200, r)
poll = r["message"]["poll"]
check("the poll carries its question",
      poll["question"] == "Where are we ordering from?", poll)
check("the poll carries its options", len(poll["options"]) == 3, poll)
check("a new poll has no votes", poll["total"] == 0 and poll["voters"] == 0, poll)
check("the author is known", poll["isAuthor"] is True, poll)

s, r = owner.call("POST", f"/api/polls/{poll['id']}/vote", {"choice": 0})
voted = r["message"]["poll"]
check("a vote is counted", voted["options"][0]["votes"] == 1, voted["options"])
check("your own vote is marked", voted["options"][0]["mine"] is True, voted["options"])
check("a single vote is 100%", voted["options"][0]["share"] == 100, voted["options"])

s, r = owner.call("POST", f"/api/polls/{poll['id']}/vote", {"choice": 1})
moved = r["message"]["poll"]
check("one vote each: a new pick moves the old one",
      moved["options"][0]["votes"] == 0 and moved["options"][1]["votes"] == 1,
      moved["options"])
check("the voter count stays at one", moved["voters"] == 1, moved)

s, r = owner.call("POST", f"/api/polls/{poll['id']}/vote", {"choice": 1})
check("voting for your pick again takes it back",
      r["message"]["poll"]["options"][1]["votes"] == 0, r["message"]["poll"])

s, r = owner.call("POST", f"/api/polls/{poll['id']}/vote", {"choice": 9})
check("you can't vote for an option that isn't there", s == 400, (s, r))
s, r = outsider.call("POST", f"/api/polls/{poll['id']}/vote", {"choice": 0})
check("outsiders can't vote", s == 403, (s, r))

member.call("POST", f"/api/polls/{poll['id']}/vote", {"choice": 0})
owner.call("POST", f"/api/polls/{poll['id']}/vote", {"choice": 0})
_, r = owner.call("POST", f"/api/polls/{poll['id']}/vote", {"choice": 0})
# (voted, then took it back — leaving just the member's vote)
_, r = member.call("GET", f"/api/channels/{cid}/messages")
current = [m for m in r["messages"] if m.get("poll")][-1]["poll"]
check("votes from other people are counted",
      current["options"][0]["votes"] == 1, current["options"])

# multiple choice
s, r = owner.call("POST", f"/api/channels/{cid}/polls",
                  {"question": "Toppings?", "options": ["Cheese", "Ham", "Olives"],
                   "multi": True})
multi = r["message"]["poll"]
check("a multiple-choice poll says so", multi["multi"] is True, multi)
owner.call("POST", f"/api/polls/{multi['id']}/vote", {"choice": 0})
_, r = owner.call("POST", f"/api/polls/{multi['id']}/vote", {"choice": 2})
both = r["message"]["poll"]
check("multiple choice keeps both votes",
      both["options"][0]["votes"] == 1 and both["options"][2]["votes"] == 1,
      both["options"])
check("one person is still one voter", both["voters"] == 1, both)

s, r = member.call("POST", f"/api/polls/{multi['id']}/close")
check("only the author closes a poll", s == 403, (s, r))
s, r = owner.call("POST", f"/api/polls/{multi['id']}/close")
check("the author can close a poll", s == 200 and r["message"]["poll"]["closed"], r)
s, r = owner.call("POST", f"/api/polls/{multi['id']}/vote", {"choice": 1})
check("a closed poll takes no more votes", s == 409, (s, r))

# =================================================================== reset
_, w = member.call("GET", "/api/wallet")
rich = w["coins"]
check("the member is holding something first", rich > 2000, rich)

s, r = member.call("POST", f"/api/guilds/{gid}/reset")
check("only the owner can reset", s == 403, (s, r))

s, r = owner.call("POST", f"/api/guilds/{gid}/reset")
check("the owner can reset", s == 200, r)
_, w = member.call("GET", "/api/wallet")
check("balances go back to the start", w["coins"] == 2000, w["coins"])
check("records are wiped", w["stats"]["wins"] == 0 and w["stats"]["losses"] == 0,
      w["stats"])
check("the daily claim is available again", w["canClaim"] is True, w)
check("perks survive a reset", "glow" in w["perks"], w["perks"])

names = [c["name"] for c in channels_of(member, gid)]
check("a reset doesn't take the lounge away", "sana-lounge" in names, names)

print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
if FAIL:
    print("FAILED: " + ", ".join(FAIL))
    sys.exit(1)
