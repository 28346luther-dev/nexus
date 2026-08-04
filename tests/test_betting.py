"""Poker betting (check / call / raise / fold), blackjack splits and roulette."""
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


def top_up(client, times=6):
    """Games need more than the 2,000 everyone starts with."""
    for _ in range(times):
        client.call("POST", "/api/wallet/work")
        # Only the first works — the rest are on the cooldown — so claim too.
    client.call("POST", "/api/wallet/claim")


host = signup("Better", "bet1@x.com")
p2 = signup("Caller", "bet2@x.com")

_, g = host.call("POST", "/api/guilds", {"name": "Bet Room"})
gid, cid = g["guildId"], g["channelId"]
_, inv = host.call("POST", f"/api/guilds/{gid}/invites", {})
p2.call("POST", f"/api/invites/{inv['code']}/join")


def board(client, game_id):
    _, r = client.call("GET", f"/api/channels/{cid}/messages")
    for m in r["messages"]:
        if m.get("game") and m["game"]["id"] == game_id:
            return m["game"]
    return None


# ============================================================ poker betting
_, r = host.call("POST", f"/api/channels/{cid}/poker", {"bet": 100})
pid = r["message"]["game"]["id"]
p2.call("POST", f"/api/games/{pid}/poker/join")
_, r = host.call("POST", f"/api/games/{pid}/poker/deal")
t = r["message"]["game"]
check("a fresh street has nothing to call", t["toCall"] == 0, t)
check("nothing to match at the top of a street", t["toMatch"] == 0, t)

_, before = host.call("GET", "/api/wallet")
_, r = host.call("POST", f"/api/games/{pid}/poker/action", {"action": "check"})
check("checking is allowed", r.get("wallet") is not None, r)
_, after = host.call("GET", "/api/wallet")
check("checking costs nothing", after["coins"] == before["coins"],
      (before["coins"], after["coins"]))

# The other player raises, which puts the host back in the hand.
_, w2before = p2.call("GET", "/api/wallet")
_, r = p2.call("POST", f"/api/games/{pid}/poker/action",
               {"action": "raise", "raise": 100})
check("raising is accepted", r.get("wallet") is not None, r)
check("a raise costs the raise", r["wallet"]["coins"] == w2before["coins"] - 100,
      (w2before["coins"], r["wallet"]["coins"]))

t = board(host, pid)
check("a raise holds the street open", t["stage"] == "flop", t["stage"])
check("the raise sets a price to match", t["toMatch"] == 100, t)
check("the player who checked owes the raise", t["toCall"] == 100, t)

s, r = host.call("POST", f"/api/games/{pid}/poker/action", {"action": "check"})
check("you can't check a raise away", s == 400, (s, r))

_, before = host.call("GET", "/api/wallet")
_, r = host.call("POST", f"/api/games/{pid}/poker/action", {"action": "call"})
check("calling costs exactly what's owed",
      r["wallet"]["coins"] == before["coins"] - 100,
      (before["coins"], r["wallet"]["coins"]))
t = r["message"]["game"]
check("the street closes once the raise is called", t["stage"] == "turn", t["stage"])
check("the next street starts free again", t["toMatch"] == 0 and t["toCall"] == 0, t)
check("the pot holds every chip put in",
      t["pot"] == sum(s["contributed"] for s in t["seats"]), (t["pot"], t["seats"]))

s, r = host.call("POST", f"/api/games/{pid}/poker/action",
                 {"action": "raise", "raise": 7})
check("a raise must be whole antes", s == 400, (s, r))
s, r = host.call("POST", f"/api/games/{pid}/poker/action",
                 {"action": "raise", "raise": 100000})
check("a raise is capped", s == 400, (s, r))

# 'stay' is what old cards send; it should still mean "call".
_, r = host.call("POST", f"/api/games/{pid}/poker/action", {"action": "stay"})
check("the old 'stay' action still works", r.get("message") is not None, r)

# ================================================= blackjack split & double
top_up(host)
_, w = host.call("GET", "/api/wallet")
check("a shift pays out", w["coins"] > 2000, w["coins"])
check("the wallet reports the next shift", "workIn" in w, w)

s, r = host.call("POST", "/api/wallet/work")
check("you can't work twice in an hour", s == 429, (s, r))

# Deal until a hand turns up that can be split, so the test isn't a coin flip.
split_game = None
for _ in range(60):
    _, r = host.call("POST", f"/api/channels/{cid}/games", {"mode": "cpu", "bet": 100})
    game = r["message"]["game"]
    if game["status"] == "playing" and game["canSplit"]:
        split_game = game
        break
check("a splittable hand turns up", split_game is not None)

if split_game:
    _, before = host.call("GET", "/api/wallet")
    _, r = host.call("POST", f"/api/games/{split_game['id']}/action", {"action": "split"})
    game = r["message"]["game"]
    check("splitting deals a second hand", len(game["seats"]["host"]["hands"]) == 2,
          game["seats"]["host"])
    check("splitting stakes a second bet",
          r["wallet"]["coins"] == before["coins"] - 100,
          (before["coins"], r["wallet"]["coins"]))
    check("each split hand has two cards",
          all(len(h["cards"]) == 2 for h in game["seats"]["host"]["hands"]),
          game["seats"]["host"]["hands"])
    check("only one split hand is live at a time",
          sum(1 for h in game["seats"]["host"]["hands"] if h["active"]) <= 1,
          game["seats"]["host"]["hands"])
    # Play both hands out.
    for _ in range(8):
        board_state = board(host, split_game["id"])
        if board_state["status"] != "playing":
            break
        host.call("POST", f"/api/games/{split_game['id']}/action", {"action": "stand"})
    done = board(host, split_game["id"])
    check("a split hand settles", done["status"] == "finished", done["status"])
    check("every hand gets its own result",
          all(h["result"] for h in done["seats"]["host"]["hands"]),
          done["seats"]["host"]["hands"])
    s, r = host.call("POST", f"/api/games/{split_game['id']}/action", {"action": "hit"})
    check("a settled hand takes no more actions", s == 409, (s, r))

# Doubling
double_game = None
for _ in range(30):
    _, r = host.call("POST", f"/api/channels/{cid}/games", {"mode": "cpu", "bet": 100})
    game = r["message"]["game"]
    if game["status"] == "playing" and game["canDouble"]:
        double_game = game
        break
check("a doubleable hand turns up", double_game is not None)

if double_game:
    _, before = host.call("GET", "/api/wallet")
    _, r = host.call("POST", f"/api/games/{double_game['id']}/action", {"action": "double"})
    game = r["message"]["game"]
    check("doubling takes a second stake",
          r["wallet"]["coins"] <= before["coins"] - 100 + 400,
          (before["coins"], r["wallet"]["coins"]))
    check("doubling ends the hand", game["status"] == "finished", game["status"])
    hand = game["seats"]["host"]["hands"][0]
    check("a doubled hand draws exactly one card", len(hand["cards"]) == 3, hand)
    check("a doubled hand is marked", hand["doubled"] is True, hand)
    check("a doubled hand stakes twice", hand["stake"] == 200, hand)
    s, r = host.call("POST", f"/api/games/{double_game['id']}/action", {"action": "double"})
    check("you can't double a finished hand", s == 409, (s, r))

# Doubling is refused in a 1v1, where the stakes are matched.
_, r = host.call("POST", f"/api/channels/{cid}/games", {"mode": "pvp", "bet": 50})
pvp = r["message"]["game"]["id"]
p2.call("POST", f"/api/games/{pvp}/join")
s, r = host.call("POST", f"/api/games/{pvp}/action", {"action": "double"})
check("no doubling in a 1v1", s == 400, (s, r))

# ================================================================= roulette
s, r = host.call("POST", f"/api/channels/{cid}/roulette", {"kind": "purple", "bet": 100})
check("an unknown bet is refused", s == 400, (s, r))
s, r = host.call("POST", f"/api/channels/{cid}/roulette", {"kind": "number",
                                                           "number": 44, "bet": 100})
check("a number off the wheel is refused", s == 400, (s, r))
s, r = host.call("POST", f"/api/channels/{cid}/roulette", {"kind": "red", "bet": 5})
check("under the minimum is refused", s == 400, (s, r))

_, before = host.call("GET", "/api/wallet")
s, r = host.call("POST", f"/api/channels/{cid}/roulette", {"kind": "red", "bet": 100})
check("the wheel spins", s == 200, r)
spin = r["message"]["game"]
check("a pocket comes up", 0 <= spin["number"] <= 36, spin)
check("the pocket has a colour",
      spin["colour"] in ("red", "black", "green"), spin)
check("an even-money bet pays 1 to 1", spin["pays"] == 1, spin)
won = spin["profit"] > 0
check("red wins exactly when red comes up",
      won == (spin["colour"] == "red"), spin)
check("the wallet matches the result",
      r["wallet"]["coins"] == before["coins"] + spin["profit"],
      (before["coins"], r["wallet"]["coins"], spin["profit"]))

s, r = host.call("POST", f"/api/channels/{cid}/roulette",
                 {"kind": "number", "number": 17, "bet": 100})
spin = r["message"]["game"]
check("a straight number pays 35 to 1", spin["pays"] == 35, spin)
check("a straight number only wins on itself",
      (spin["profit"] > 0) == (spin["number"] == 17), spin)

print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
if FAIL:
    print("FAILED: " + ", ".join(FAIL))
    sys.exit(1)
