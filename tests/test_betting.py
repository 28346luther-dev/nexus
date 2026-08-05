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

# ==================================================== all in, not forced out
import sqlite3                                                    # noqa: E402
DB_PATH = sys.argv[2] if len(sys.argv) > 2 else "/tmp/nexus_test.db"


def set_coins(client, amount):
    conn = sqlite3.connect(DB_PATH)
    conn.execute("UPDATE users SET coins = ? WHERE id = ?", (amount, client.uid))
    conn.commit()
    conn.close()


set_coins(host, 100_000)
set_coins(p2, 100_000)
_, r = host.call("POST", f"/api/channels/{cid}/poker", {"bet": 100})
allin = r["message"]["game"]["id"]
p2.call("POST", f"/api/games/{allin}/poker/join")
host.call("POST", f"/api/games/{allin}/poker/deal")

# Leave the host with less than the bet they are about to face.
set_coins(host, 50)
p2.call("POST", f"/api/games/{allin}/poker/action", {"action": "raise", "raise": 1000})
s, r = host.call("POST", f"/api/games/{allin}/poker/action", {"action": "call"})
check("a short stack is not refused", s == 200, (s, r))
t = r["message"]["game"]
mine = [x for x in t["seats"] if x["isYou"]][0]
check("it puts in what it has", mine["allIn"] is True, mine)
check("and stays in the hand", mine["folded"] is False, mine)
check("the wallet is emptied, not overdrawn", r["wallet"]["coins"] == 0,
      r["wallet"]["coins"])

# The hand plays on, and another raise must not demand more from them.
_, before = host.call("GET", "/api/wallet")
for _ in range(6):
    t = board(host, allin)
    if t["status"] != "playing":
        break
    if t["yourTurn"]:
        s, r = host.call("POST", f"/api/games/{allin}/poker/action", {"action": "check"})
        check("an all-in player is never asked to act again", False, (s, r))
        break
    p2.call("POST", f"/api/games/{allin}/poker/action", {"action": "raise", "raise": 500})
_, after = host.call("GET", "/api/wallet")
check("nothing more is taken from an empty wallet",
      after["coins"] == before["coins"], (before["coins"], after["coins"]))

done = board(host, allin)
check("the hand reaches an end", done["status"] == "finished", done["status"])
mine = [x for x in done["seats"] if x["isYou"]][0]
check("the short stack was never forced to fold", mine["folded"] is False, mine)
if mine["won"]:
    check("a short stack wins only what it matched",
          mine["wonAmount"] <= done["pot"], (mine["wonAmount"], done["pot"]))

# ================================================================= begging
set_coins(host, 50_000)
set_coins(p2, 50_000)
_, before = host.call("GET", "/api/wallet")
s, r = host.call("POST", f"/api/channels/{cid}/beg")
check("begging works", s == 200, r)
beg = r["message"]["game"]
check("the card says who is asking", beg["player"]["id"] == host.uid, beg)
check("the Frontman either gives or doesn't",
      beg["botGave"] == 0 or 100 <= beg["botGave"] <= 1000, beg)
check("and the wallet matches whatever it did",
      r["wallet"]["coins"] == before["coins"] + beg["botGave"],
      (before["coins"], r["wallet"]["coins"], beg["botGave"]))
check("the beggar gets no Give button", beg["canGive"] is False, beg)

s, r = host.call("POST", f"/api/channels/{cid}/beg")
check("you can't beg twice in half an hour", s == 429, (s, r))

# Other players can give whatever they like.
theirs = board(p2, beg["id"])
check("everyone else can give", theirs["canGive"] is True, theirs)
s, r = p2.call("POST", f"/api/games/{beg['id']}/beg/give", {"amount": 2500})
check("a donation lands", s == 200, r)
check("it comes out of the giver's pocket",
      r["wallet"]["coins"] == 50_000 - 2500, r["wallet"]["coins"])
card = r["message"]["game"]
check("and is listed on the card",
      len(card["donations"]) == 1 and card["donations"][0]["amount"] == 2500,
      card["donations"])
_, w = host.call("GET", "/api/wallet")
check("the beggar is better off",
      w["coins"] == before["coins"] + beg["botGave"] + 2500, w["coins"])

s, r = host.call("POST", f"/api/games/{beg['id']}/beg/give", {"amount": 100})
check("you can't beg from yourself", s == 400, (s, r))
s, r = p2.call("POST", f"/api/games/{beg['id']}/beg/give", {"amount": 0})
check("nothing is not a donation", s == 400, (s, r))
s, r = p2.call("POST", f"/api/games/{beg['id']}/beg/give", {"amount": -500})
check("nor is a negative one", s == 400, (s, r))
s, r = p2.call("POST", f"/api/games/{beg['id']}/beg/give", {"amount": 99_000_000})
check("you can't give what you haven't got", s == 400, (s, r))

# ======================================================= one card, replayed
def cards_in_channel(client):
    _, r = client.call("GET", f"/api/channels/{cid}/messages")
    return [m for m in r["messages"] if m.get("game")]


start = len(cards_in_channel(host))
_, r = host.call("POST", f"/api/channels/{cid}/slots", {"bet": 10})
first = r["message"]
check("a spin posts a card", len(cards_in_channel(host)) == start + 1)
check("the card knows its own message",
      first["game"]["messageId"] == first["id"], first["game"])

_, r = host.call("POST", f"/api/channels/{cid}/slots",
                 {"bet": 10, "replace": first["id"]})
check("replaying reuses the same card", r["message"]["id"] == first["id"],
      (first["id"], r["message"]["id"]))
check("and doesn't add another", len(cards_in_channel(host)) == start + 1,
      len(cards_in_channel(host)))
check("the card shows the new spin",
      r["message"]["game"]["id"] != first["game"]["id"], r["message"]["game"])

_, r = host.call("POST", f"/api/channels/{cid}/slots", {"bet": 10})
check("an ordinary spin still posts its own card",
      len(cards_in_channel(host)) == start + 2, len(cards_in_channel(host)))

# Somebody else's card is not yours to take over.
mine = cards_in_channel(host)[-1]
_, r = p2.call("POST", f"/api/channels/{cid}/slots",
               {"bet": 10, "replace": mine["id"]})
check("you can't replay someone else's card", r["message"]["id"] != mine["id"],
      (mine["id"], r["message"]["id"]))

# Nor a hand that is still being played.
_, r = host.call("POST", f"/api/channels/{cid}/games", {"mode": "pvp", "bet": 10})
live = r["message"]
_, r = host.call("POST", f"/api/channels/{cid}/slots",
                 {"bet": 10, "replace": live["id"]})
check("you can't replay a table that's still open",
      r["message"]["id"] != live["id"], (live["id"], r["message"]["id"]))

print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
if FAIL:
    print("FAILED: " + ", ".join(FAIL))
    sys.exit(1)
