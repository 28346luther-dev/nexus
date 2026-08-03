"""Tests for the multiplayer poker table."""
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


host = signup("Dealer", "pk1@x.com")
p2 = signup("Player Two", "pk2@x.com")
p3 = signup("Player Three", "pk3@x.com")
outsider = signup("Nosy", "pk4@x.com")

_, g = host.call("POST", "/api/guilds", {"name": "Card Room"})
gid, cid = g["guildId"], g["channelId"]
_, inv = host.call("POST", f"/api/guilds/{gid}/invites", {})
p2.call("POST", f"/api/invites/{inv['code']}/join")
p3.call("POST", f"/api/invites/{inv['code']}/join")


def board(client, game_id):
    _, r = client.call("GET", f"/api/channels/{cid}/messages")
    for m in r["messages"]:
        if m.get("game") and m["game"]["id"] == game_id:
            return m["game"]
    return None


# ----------------------------------------------------------------- opening
s, r = host.call("POST", f"/api/channels/{cid}/poker", {"bet": 5})
check("ante below the minimum rejected", s == 400, (s, r))
s, w = host.call("GET", "/api/wallet")
check("rejected ante refunded", w["coins"] == 2000, w["coins"])

s, r = host.call("POST", f"/api/channels/{cid}/poker", {"bet": 100})
check("table opened", s == 200, r)
tbl = r["message"]["game"]
pid = tbl["id"]
check("host is seated", len(tbl["seats"]) == 1, tbl["seats"])
check("host paid the ante into the pot", tbl["pot"] == 100, tbl)
check("host balance reduced", r["wallet"]["coins"] == 1900, r["wallet"])
check("table starts waiting", tbl["stage"] == "waiting", tbl)
check("cannot deal alone", tbl["canDeal"] is False, tbl)

s, r = host.call("POST", f"/api/games/{pid}/poker/deal")
check("dealing needs a second player", s == 400, (s, r))

# ------------------------------------------------------------------ joining
s, r = host.call("POST", f"/api/games/{pid}/poker/join")
check("host can't join twice", s == 409, (s, r))
s, r = outsider.call("POST", f"/api/games/{pid}/poker/join")
check("non-member can't join", s == 403, (s, r))

s, r = p2.call("POST", f"/api/games/{pid}/poker/join")
check("second player joins", s == 200, r)
check("their ante is taken", r["wallet"]["coins"] == 1900, r["wallet"])
check("pot grows", r["message"]["game"]["pot"] == 200, r["message"]["game"])
s, r = p3.call("POST", f"/api/games/{pid}/poker/join")
check("third player joins", s == 200 and r["message"]["game"]["pot"] == 300, r["message"]["game"])

# ------------------------------------------------------------------ dealing
s, r = p2.call("POST", f"/api/games/{pid}/poker/deal")
check("only the host may deal", s == 403, (s, r))
s, r = host.call("POST", f"/api/games/{pid}/poker/deal")
check("host deals", s == 200, r)
t = r["message"]["game"]
check("stage moves to the flop", t["stage"] == "flop", t["stage"])
check("flop is three cards", len(t["board"]) == 3, t["board"])

# ------------------------------------------------------------- hole secrecy
mine = board(host, pid)
own = [s for s in mine["seats"] if s["isYou"]][0]
others = [s for s in mine["seats"] if not s["isYou"]]
check("you see your own hole cards", "??" not in own["hole"] and len(own["hole"]) == 2, own)
check("other hole cards are hidden", all(set(o["hole"]) == {"??"} for o in others), others)

spectator = signup("Watcher", "pk5@x.com")
spectator.call("POST", f"/api/invites/{inv['code']}/join")
spec = board(spectator, pid)
check("spectators see no hole cards",
      all(set(s["hole"]) == {"??"} for s in spec["seats"]), spec["seats"])
check("spectators can't act", spec["yourTurn"] is False, spec)
s, r = spectator.call("POST", f"/api/games/{pid}/poker/action", {"action": "stay"})
check("spectator action refused", s == 403, (s, r))

# ------------------------------------------------------------------ betting
s, r = host.call("POST", f"/api/games/{pid}/poker/action", {"action": "bogus"})
check("unknown action rejected", s == 400, (s, r))

s, r = host.call("POST", f"/api/games/{pid}/poker/action", {"action": "stay"})
check("host stays", s == 200, r)
check("staying costs the ante again", r["wallet"]["coins"] == 1800, r["wallet"])
s, r = host.call("POST", f"/api/games/{pid}/poker/action", {"action": "stay"})
check("can't act twice in a round", s == 409, (s, r))

t = board(host, pid)
check("still on the flop until everyone acts", t["stage"] == "flop", t["stage"])

p2.call("POST", f"/api/games/{pid}/poker/action", {"action": "stay"})
s, r = p3.call("POST", f"/api/games/{pid}/poker/action", {"action": "stay"})
t = r["message"]["game"]
check("round advances once all have acted", t["stage"] == "turn", t["stage"])
check("turn adds a fourth card", len(t["board"]) == 4, t["board"])
check("pot collected the round", t["pot"] == 600, t["pot"])

# --------------------------------------------------------------- to the end
for who in (host, p2, p3):
    who.call("POST", f"/api/games/{pid}/poker/action", {"action": "stay"})
t = board(host, pid)
check("river dealt", t["stage"] == "river" and len(t["board"]) == 5, t)

for who in (host, p2, p3):
    s, r = who.call("POST", f"/api/games/{pid}/poker/action", {"action": "stay"})
t = board(host, pid)
check("reaches showdown", t["stage"] == "showdown", t["stage"])
check("game marked finished", t["status"] == "finished", t["status"])
check("all hole cards revealed",
      all("??" not in s["hole"] for s in t["seats"]), t["seats"])
check("a winner is named", t["result"] and t["result"]["winners"], t["result"])
check("everyone's hand is described",
      all("hand" in s for s in t["seats"]), t["seats"])

pot = t["pot"]
check("pot is the sum of contributions",
      pot == sum(s["contributed"] for s in t["seats"]), (pot, t["seats"]))

# Winner got paid, and the payout happens exactly once
winner_ids = set(t["result"]["winners"])
by_id = {host.uid: host, p2.uid: p2, p3.uid: p3}
for uid in winner_ids:
    _, w = by_id[uid].call("GET", "/api/wallet")
    check(f"winner {uid} is up on the hand", w["stats"]["wins"] == 1, w["stats"])
for uid in set(by_id) - winner_ids:
    _, w = by_id[uid].call("GET", "/api/wallet")
    check(f"loser {uid} recorded", w["stats"]["losses"] == 1, w["stats"])

s, r = host.call("POST", f"/api/games/{pid}/poker/action", {"action": "stay"})
check("finished table rejects further action", s == 409, (s, r))

# --------------------------------------------------------- folding shortcut
s, r = host.call("POST", f"/api/channels/{cid}/poker", {"bet": 50})
p2id = r["message"]["game"]["id"]
p2.call("POST", f"/api/games/{p2id}/poker/join")
host.call("POST", f"/api/games/{p2id}/poker/deal")
_, w_before = p2.call("GET", "/api/wallet")
host.call("POST", f"/api/games/{p2id}/poker/action", {"action": "fold"})
t = board(p2, p2id)
check("everyone else folding ends the hand", t["stage"] == "showdown", t["stage"])
check("last player standing wins", t["result"]["winners"] == [p2.uid], t["result"])
_, w_after = p2.call("GET", "/api/wallet")
check("winner collects the pot", w_after["coins"] == w_before["coins"] + t["pot"],
      (w_before["coins"], w_after["coins"], t["pot"]))

# ------------------------------------------------------------------ dm rule
_, dm = host.call("POST", "/api/dms", {"userId": p2.uid})
s, r = host.call("POST", f"/api/channels/{dm['channelId']}/poker", {"bet": 100})
check("poker refused in a dm", s == 400, (s, r))

print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
if FAIL:
    print("FAILED: " + ", ".join(FAIL))
