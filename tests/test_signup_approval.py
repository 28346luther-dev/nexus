"""Sign-ups held for an admin's approval."""
import json
import sqlite3
import sys
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


def signup(name, email, full_name="A Real Person"):
    c = C()
    s, r = c.call("POST", "/api/register", {
        "username": name, "email": email, "password": "hunter2hunter2",
        "fullName": full_name,
    })
    if s == 200:
        c.uid = r["user"]["id"]
    return c, s, r


def make_admin(uid):
    """Promote directly: which account is the admin depends on the server's
    configuration, and this test needs one it controls."""
    conn = sqlite3.connect(DB_PATH)
    conn.execute("UPDATE users SET is_admin = 1, approved = 1 WHERE id = ?", (uid,))
    conn.commit()
    conn.close()


# --------------------------------------------------------------- the form
c = C()
s, r = c.call("POST", "/api/register",
              {"username": "Nameless", "email": "sg0@x.com",
               "password": "hunter2hunter2"})
check("a name is required to sign up", s == 400, (s, r))
s, r = c.call("POST", "/api/register",
              {"username": "Nameless", "email": "sg0@x.com",
               "password": "hunter2hunter2", "fullName": "X"})
check("and it has to be a real one", s == 400, (s, r))

# ------------------------------------------------------------- the admin
admin, s, r = signup("Boss", "sgadmin@x.com", "The Boss")
check("the admin's account exists", s == 200, r)
make_admin(admin.uid)
_, me = admin.call("GET", "/api/me")
check("the admin is flagged as one", me["user"]["isAdmin"] is True, me["user"])
check("the admin is approved", me["user"]["approved"] is True, me["user"])

# ------------------------------------------------------------ a new joiner
joiner, s, r = signup("Hopeful", "sgnew@x.com", "Alex Sawyer")
check("signing up works", s == 200, r)
check("but the account is not approved", r["user"]["approved"] is False, r["user"])
check("and it knows it is waiting", r["user"]["signupStatus"] == "pending", r["user"])
check("the name given is kept", r["user"]["fullName"] == "Alex Sawyer", r["user"])

s, r = joiner.call("GET", "/api/guilds")
check("a waiting account can't list servers", s == 403, (s, r))
check("and is told why, in a way the page can act on", r.get("pending") is True, r)
s, r = joiner.call("POST", "/api/guilds", {"name": "Mine"})
check("nor create one", s == 403, (s, r))
s, r = joiner.call("GET", "/api/wallet")
check("nor touch the economy", s == 403, (s, r))
s, r = joiner.call("GET", "/api/me")
check("but can still ask who it is", s == 200 and r["user"]["username"] == "Hopeful", r)

# ------------------------------------------------------- telling the admin
s, dms = admin.call("GET", "/api/dms")
bot_dm = next((d for d in dms["dms"] if d["user"]["isBot"]), None)
check("the Frontman opens a DM with the admin", bot_dm is not None, dms)
check("and it is unread", bot_dm and bot_dm["unread"] >= 1, bot_dm)

s, msgs = admin.call("GET", f"/api/channels/{bot_dm['channelId']}/messages")
cards = [m for m in msgs["messages"] if m.get("signup")]
check("there is a card to act on", len(cards) >= 1, msgs)
card = cards[-1]["signup"]
check("the card is waiting on a decision", card["status"] == "pending", card)
check("it shows the name they gave",
      card["user"]["fullName"] == "Alex Sawyer", card["user"])
check("and the address they used", card["user"]["email"] == "sgnew@x.com", card["user"])
check("the admin can decide it", card["canDecide"] is True, card)

s, r = joiner.call("GET", "/api/signups")
check("a waiting account can't read the queue", s == 403, (s, r))
s, r = joiner.call("POST", f"/api/signups/{card['id']}/decide", {"verdict": "approve"})
check("nor approve itself", s == 403, (s, r))

outsider, _, _ = signup("Nosy", "sgnosy@x.com", "Nosy Parker")
make_admin(outsider.uid)          # an admin, so past the pending gate
conn = sqlite3.connect(DB_PATH)
conn.execute("UPDATE users SET is_admin = 0 WHERE id = ?", (outsider.uid,))
conn.commit()
conn.close()
s, r = outsider.call("POST", f"/api/signups/{card['id']}/decide", {"verdict": "approve"})
check("an ordinary member can't approve anyone", s == 403, (s, r))

# --------------------------------------------------------------- deciding
s, r = admin.call("GET", "/api/signups")
check("the admin can see the queue", s == 200, r)
check("the waiting account is in it",
      any(a["id"] == card["id"] for a in r["signups"]), r["signups"])

s, r = admin.call("POST", f"/api/signups/{card['id']}/decide", {"verdict": "sideways"})
check("the verdict has to be one of two things", s == 400, (s, r))

s, r = admin.call("POST", f"/api/signups/{card['id']}/decide", {"verdict": "approve"})
check("the admin approves", s == 200 and r["signup"]["status"] == "approved", r)
check("the card says who decided", r["signup"]["decidedBy"] == "Boss", r["signup"])
check("and offers no more buttons", r["signup"]["canDecide"] is False, r["signup"])

_, r = joiner.call("GET", "/api/me")
check("the account is approved", r["user"]["approved"] is True, r["user"])
check("and stops being marked as waiting", r["user"]["signupStatus"] is None, r["user"])
s, r = joiner.call("GET", "/api/guilds")
check("and can use the site", s == 200, (s, r))

s, r = admin.call("POST", f"/api/signups/{card['id']}/decide", {"verdict": "decline"})
check("a decision can't be taken twice", s == 409, (s, r))

s, r = admin.call("GET", "/api/signups")
check("an approved account leaves the queue",
      not any(a["id"] == card["id"] for a in r["signups"]), r["signups"])

# --------------------------------------------------------------- declining
turned, _, _ = signup("Rejected", "sgno@x.com", "Someone Else")
_, dms = admin.call("GET", "/api/dms")
bot_dm = next(d for d in dms["dms"] if d["user"]["isBot"])
_, msgs = admin.call("GET", f"/api/channels/{bot_dm['channelId']}/messages")
theirs = [m for m in msgs["messages"]
          if m.get("signup") and m["signup"]["user"]
          and m["signup"]["user"]["email"] == "sgno@x.com"][-1]["signup"]

s, r = admin.call("POST", f"/api/signups/{theirs['id']}/decide", {"verdict": "decline"})
check("the admin can turn someone away",
      s == 200 and r["signup"]["status"] == "declined", r)
_, r = turned.call("GET", "/api/me")
check("a declined account stays shut out", r["user"]["approved"] is False, r["user"])
check("and is told it was declined", r["user"]["signupStatus"] == "declined", r["user"])
s, r = turned.call("GET", "/api/guilds")
check("with no way into the site", s == 403, (s, r))

s, r = turned.call("POST", "/api/register", {
    "username": "Rejected", "email": "sgno@x.com",
    "password": "hunter2hunter2", "fullName": "Someone Else"})
check("and can't simply sign up again", s == 409, (s, r))

# ------------------------------------------------------- the admin panel
s, r = joiner.call("GET", "/api/admin/users")
check("an ordinary member can't list everyone", s == 403, (s, r))
s, r = admin.call("GET", "/api/admin/users")
check("the admin can", s == 200, r)
everyone = r["users"]
check("the bot is left out of the list",
      all(not u["tag"].startswith("Frontman") for u in everyone), everyone)
me_row = [u for u in everyone if u["id"] == admin.uid][0]
check("the admin's own row is marked", me_row["isYou"] is True, me_row)
check("and flagged as an admin", me_row["isAdmin"] is True, me_row)
theirs = [u for u in everyone if u["id"] == joiner.uid][0]
check("the list carries the name given at sign-up",
      theirs["fullName"] == "Alex Sawyer", theirs)
check("and what would go with them",
      "owns" in theirs and "messages" in theirs, theirs)

s, r = admin.call("DELETE", f"/api/admin/users/{admin.uid}")
check("the admin can't delete themselves", s == 400, (s, r))
s, r = joiner.call("DELETE", f"/api/admin/users/{admin.uid}")
check("nor can anyone else", s == 403, (s, r))

# A member with a server of their own: it goes when they do.
victim = signup("Doomed", "sgbye@x.com", "Going Away")[0]
conn = sqlite3.connect(DB_PATH)
conn.execute("UPDATE users SET approved = 1 WHERE id = ?", (victim.uid,))
conn.commit()
conn.close()
_, g = victim.call("POST", "/api/guilds", {"name": "Their Server"})
victim.call("POST", f"/api/channels/{g['channelId']}/messages", {"content": "hello"})

_, r = admin.call("GET", "/api/admin/users")
row = [u for u in r["users"] if u["id"] == victim.uid][0]
check("their server is counted", row["owns"] == 1, row)
check("and their messages", row["messages"] >= 1, row)

s, r = admin.call("DELETE", f"/api/admin/users/{victim.uid}")
check("the admin can delete an account", s == 200, (s, r))
check("and is told what went", r["deleted"]["servers"] == 1, r["deleted"])

_, r = admin.call("GET", "/api/admin/users")
check("they are gone from the list",
      not any(u["id"] == victim.uid for u in r["users"]), r["users"])
s, r = victim.call("GET", "/api/me")
check("their session no longer signs anybody in", not r.get("user"), r)
s, r = victim.call("POST", "/api/login",
                   {"email": "sgbye@x.com", "password": "hunter2hunter2"})
check("and the account can't sign back in", s == 401, (s, r))
s, r = admin.call("DELETE", f"/api/admin/users/{victim.uid}")
check("deleting a ghost is refused", s == 404, (s, r))

print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
if FAIL:
    print("FAILED: " + ", ".join(FAIL))
    sys.exit(1)
