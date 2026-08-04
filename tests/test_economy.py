"""Tests for Sana Coin, betting, slots, ranks and the leaderboard."""
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


host, guest, watcher = C(), C(), C()
_, rh = host.call("POST", "/api/register", {"username": "Highroller", "email": "ec1@x.com", "password": "hunter2hunter2"})
_, rg = guest.call("POST", "/api/register", {"username": "Challenger", "email": "ec2@x.com", "password": "hunter2hunter2"})
_, rw = watcher.call("POST", "/api/register", {"username": "Watcher", "email": "ec3@x.com", "password": "hunter2hunter2"})

_, g = host.call("POST", "/api/guilds", {"name": "Casino"})
gid, cid = g["guildId"], g["channelId"]
_, inv = host.call("POST", f"/api/guilds/{gid}/invites", {})
guest.call("POST", f"/api/invites/{inv['code']}/join")
watcher.call("POST", f"/api/invites/{inv['code']}/join")

# ------------------------------------------------------------------ wallet
s, w = host.call("GET", "/api/wallet")
check("new account is funded", s == 200 and w["coins"] == 2000, w)
check("stats start empty", w["stats"]["wins"] == 0 and w["stats"]["losses"] == 0, w["stats"])
check("starting rank", w["stats"]["rank"] == "Rookie", w["stats"])
check("claim available immediately", w["canClaim"] is True, w)

s, r = host.call("POST", "/api/wallet/claim")
check("claim pays the daily amount", s == 200 and r["claimed"] == 5000, r)
check("balance went up", r["coins"] == 7000, r["coins"])
s, r = host.call("POST", "/api/wallet/claim")
check("second claim same day refused", s == 429, (s, r))
_, w = host.call("GET", "/api/wallet")
check("claim now on cooldown", w["canClaim"] is False and w["claimIn"] > 0, w)

# ------------------------------------------------------------------ betting
s, r = host.call("POST", f"/api/channels/{cid}/games", {"mode": "cpu", "bet": 999_999_999})
check("bet above the cap rejected", s == 400, (s, r))
s, r = host.call("POST", f"/api/channels/{cid}/games", {"mode": "cpu", "bet": -50})
check("negative bet rejected", s == 400, (s, r))
s, r = host.call("POST", f"/api/channels/{cid}/games", {"mode": "cpu", "bet": 50_000})
check("bet beyond your balance rejected", s == 400, (s, r))
_, w = host.call("GET", "/api/wallet")
check("failed bets don't move the balance", w["coins"] == 7000, w["coins"])

before = w["coins"]
s, r = host.call("POST", f"/api/channels/{cid}/games", {"mode": "cpu", "bet": 500})
check("bet accepted", s == 200, r)
game = r["message"]["game"]
check("stake shown on the game", game["bet"] == 500, game)
mid_balance = r["wallet"]["coins"]
if game["status"] == "playing":
    check("stake held while the hand is live", mid_balance == before - 500, mid_balance)
    # Play it to the end
    gid_cpu = game["id"]
    st = game["status"]
    guard = 0
    while st == "playing" and guard < 25:
        s, r = host.call("POST", f"/api/games/{gid_cpu}/action", {"action": "stand"})
        st = r["message"]["game"]["status"]
        guard += 1
    final = r["message"]["game"]
    after = r["wallet"]["coins"]
    outcome = final["outcome"]
    expected = {"win": before + 500, "push": before, "lose": before - 500}
    if outcome == "win" and final["result"]["text"] == "Blackjack!":
        expected["win"] = before + 750          # 3:2
    check(f"payout matches the {outcome}", after == expected[outcome],
          (outcome, before, after, expected[outcome]))
    _, w = host.call("GET", "/api/wallet")
    total = w["stats"]["wins"] + w["stats"]["losses"] + w["stats"]["pushes"]
    check("result recorded in stats", total == 1, w["stats"])

# Paying out twice would mint coins from nothing
s, r = host.call("POST", f"/api/games/{game['id']}/action", {"action": "stand"})
check("finished hand can't be replayed", s == 409, (s, r))

# ------------------------------------------------------------ 1v1 stakes
_, w = host.call("GET", "/api/wallet")
host_before = w["coins"]
_, w = guest.call("GET", "/api/wallet")
guest_before = w["coins"]

s, r = host.call("POST", f"/api/channels/{cid}/games", {"mode": "pvp", "bet": 300})
pid = r["message"]["game"]["id"]
check("1v1 stake held from the host", r["wallet"]["coins"] == host_before - 300, r["wallet"])

s, r = watcher.call("POST", f"/api/games/{pid}/join")
check("joiner matches the stake", s == 200 and r["wallet"]["coins"] == 2000 - 300, r.get("wallet"))

# A broke player can't sit down at a table they can't cover
poor = C()
poor.call("POST", "/api/register", {"username": "Skint", "email": "ec4@x.com", "password": "hunter2hunter2"})
poor.call("POST", f"/api/invites/{inv['code']}/join")
s, r = host.call("POST", f"/api/channels/{cid}/games", {"mode": "pvp", "bet": 5000})
if s == 400:
    check("host can't stake more than they hold", True)
else:
    big = r["message"]["game"]["id"]
    s, r = poor.call("POST", f"/api/games/{big}/join")
    check("can't join a table you can't cover", s == 400, (s, r))

# -------------------------------------------------------------- 1v1 secrecy
s, r = host.call("POST", f"/api/channels/{cid}/games", {"mode": "pvp", "bet": 0})
sec = r["message"]["game"]["id"]
guest.call("POST", f"/api/games/{sec}/join")
s, r = host.call("GET", f"/api/channels/{cid}/messages")
live = [m for m in r["messages"] if m.get("game") and m["game"]["id"] == sec][0]["game"]
check("you see your own cards", "??" not in live["seats"]["host"]["cards"], live["seats"]["host"])
check("opponent's cards are hidden mid-hand", set(live["seats"]["opp"]["cards"]) == {"??"},
      live["seats"]["opp"])
check("opponent's total is hidden too", live["seats"]["opp"]["total"] is None, live["seats"]["opp"])
s, r = watcher.call("GET", f"/api/channels/{cid}/messages")
spec = [m for m in r["messages"] if m.get("game") and m["game"]["id"] == sec][0]["game"]
check("spectators see neither hand",
      set(spec["seats"]["host"]["cards"]) == {"??"} and set(spec["seats"]["opp"]["cards"]) == {"??"},
      spec["seats"])

host.call("POST", f"/api/games/{sec}/action", {"action": "stand"})
s, r = guest.call("POST", f"/api/games/{sec}/action", {"action": "stand"})
done = r["message"]["game"]
check("both hands revealed once settled",
      "??" not in done["seats"]["host"]["cards"] and "??" not in done["seats"]["opp"]["cards"],
      done["seats"])

# ------------------------------------------------------------------- slots
_, w = host.call("GET", "/api/wallet")
before = w["coins"]
s, r = host.call("POST", f"/api/channels/{cid}/slots", {"bet": 5})
check("tiny spin rejected", s == 400, (s, r))
_, w = host.call("GET", "/api/wallet")
check("rejected spin refunds the stake", w["coins"] == before, w["coins"])

s, r = host.call("POST", f"/api/channels/{cid}/slots", {"bet": 100})
check("spin accepted", s == 200, r)
sl = r["message"]["game"]
check("three reels shown", len(sl["reels"]) == 3, sl)
check("slots game is finished immediately", sl["status"] == "finished", sl)
check("balance reflects the spin", r["wallet"]["coins"] == before - 100 + sl["payout"],
      (before, r["wallet"]["coins"], sl["payout"]))

s, r = host.call("POST", f"/api/channels/{cid}/slots", {"bet": 10_000_000})
check("slots bet above the cap rejected", s == 400, (s, r))

# ------------------------------------------------------- bank & leaderboard
s, r = guest.call("GET", f"/api/guilds/{gid}/bank")
check("bank lists everyone", s == 200 and len(r["accounts"]) >= 4, r)
check("bank sorted richest first",
      [a["coins"] for a in r["accounts"]] == sorted([a["coins"] for a in r["accounts"]], reverse=True),
      [a["coins"] for a in r["accounts"]])
check("bot excluded from the bank",
      all(a["username"] != "Frontman" for a in r["accounts"]), r["accounts"])
check("bank reports a total", r["total"] == sum(a["coins"] for a in r["accounts"]), r["total"])

s, r = C().call("GET", f"/api/guilds/{gid}/bank")
check("bank needs an account", s == 401, (s, r))
outsider = C()
outsider.call("POST", "/api/register", {"username": "Nosy", "email": "ec5@x.com", "password": "hunter2hunter2"})
s, r = outsider.call("GET", f"/api/guilds/{gid}/bank")
check("non-member can't read the bank", s == 403, (s, r))

s, r = guest.call("GET", f"/api/guilds/{gid}/leaderboard")
check("leaderboard returns players", s == 200 and len(r["players"]) >= 1, r)
check("leaderboard sorted by wins",
      [p["wins"] for p in r["players"]] == sorted([p["wins"] for p in r["players"]], reverse=True),
      [p["wins"] for p in r["players"]])
check("everyone has a rank", all(p["rank"] for p in r["players"]), r["players"])
s, r = outsider.call("GET", f"/api/guilds/{gid}/leaderboard")
check("non-member can't read the leaderboard", s == 403, (s, r))

print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
if FAIL:
    print("FAILED: " + ", ".join(FAIL))
