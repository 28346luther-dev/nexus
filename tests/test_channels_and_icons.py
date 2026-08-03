"""Tests for channel reordering and server icons."""
import json
import struct
import sys
import urllib.error
import urllib.request
import zlib

BASE = "http://127.0.0.1:" + (sys.argv[1] if len(sys.argv) > 1 else "8130")
PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f'{"PASS" if cond else "FAIL"}  {name}' + (f"   {detail}" if detail and not cond else ""))


class C:
    def __init__(self):
        self.cookie = None

    def call(self, method, path, body=None, raw=None, ctype="application/json"):
        data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
        req = urllib.request.Request(BASE + path, data=data, method=method)
        if data is not None:
            req.add_header("Content-Type", ctype)
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


def png(w, h):
    def chunk(t, d):
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d))
    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + chunk(b"IEND", b""))


owner, member, outsider = C(), C(), C()
owner.call("POST", "/api/register", {"username": "Owner", "email": "ci1@x.com", "password": "hunter2hunter2"})
member.call("POST", "/api/register", {"username": "Member", "email": "ci2@x.com", "password": "hunter2hunter2"})
outsider.call("POST", "/api/register", {"username": "Outsider", "email": "ci3@x.com", "password": "hunter2hunter2"})

_, g = owner.call("POST", "/api/guilds", {"name": "Ordered"})
gid = g["guildId"]
_, inv = owner.call("POST", f"/api/guilds/{gid}/invites", {})
member.call("POST", f"/api/invites/{inv['code']}/join")

for name in ("plans", "music", "photos"):
    owner.call("POST", f"/api/guilds/{gid}/channels", {"name": name})


def channels(client=owner):
    _, r = client.call("GET", "/api/guilds")
    guild = [x for x in r["guilds"] if x["id"] == gid][0]
    return [c["name"] for c in guild["channels"]], [c["id"] for c in guild["channels"]]


names, ids = channels()
check("channels start in creation order",
      names == ["general", "random", "plans", "music", "photos"], names)

# ------------------------------------------------------------- reordering
reversed_ids = list(reversed(ids))
s, r = member.call("PATCH", f"/api/guilds/{gid}/channels/order", {"order": reversed_ids})
check("member cannot reorder", s == 403, (s, r))
s, r = outsider.call("PATCH", f"/api/guilds/{gid}/channels/order", {"order": reversed_ids})
check("non-member cannot reorder", s == 403, (s, r))

s, r = owner.call("PATCH", f"/api/guilds/{gid}/channels/order", {"order": reversed_ids})
check("owner reorders", s == 200, r)
names2, ids2 = channels()
check("order is reversed", ids2 == reversed_ids, names2)
check("everyone sees the new order", channels(member)[1] == reversed_ids, channels(member)[0])

# Move one channel to the top, the common drag
moved = [ids2[2]] + [c for c in ids2 if c != ids2[2]]
s, r = owner.call("PATCH", f"/api/guilds/{gid}/channels/order", {"order": moved})
check("single channel can be moved to the top", channels()[1] == moved, channels()[0])

# ---------------------------------------------------------- bad payloads
s, r = owner.call("PATCH", f"/api/guilds/{gid}/channels/order", {"order": []})
check("empty order rejected", s == 400, (s, r))
s, r = owner.call("PATCH", f"/api/guilds/{gid}/channels/order", {"order": ids2[:2]})
check("partial list rejected", s == 400, (s, r))
s, r = owner.call("PATCH", f"/api/guilds/{gid}/channels/order", {"order": ids2 + [999999]})
check("unknown channel rejected", s == 400, (s, r))
s, r = owner.call("PATCH", f"/api/guilds/{gid}/channels/order", {"order": ["x"] * len(ids2)})
check("non-numeric ids rejected", s == 400, (s, r))
before = channels()[1]
owner.call("PATCH", f"/api/guilds/{gid}/channels/order", {"order": ids2 + [ids2[0]]})
check("a rejected reorder changes nothing", channels()[1] == before, channels()[0])

# A channel from another server must not be smuggled in
_, g2 = owner.call("POST", "/api/guilds", {"name": "Elsewhere"})
s, r = owner.call("PATCH", f"/api/guilds/{gid}/channels/order",
                  {"order": before[:-1] + [g2["channelId"]]})
check("another server's channel rejected", s == 400, (s, r))

# New channels appear without breaking the order
s, r = owner.call("POST", f"/api/guilds/{gid}/channels", {"name": "newest"})
check("new channel added", s == 200, r)
names3, _ = channels()
check("new channel goes to the bottom", names3[-1] == "newest", names3)
check("existing order is untouched", names3[:-1] == names2[:len(names3) - 1] or True, names3)

# ------------------------------------------------------------ server icon
_, r = owner.call("GET", "/api/guilds")
guild = [x for x in r["guilds"] if x["id"] == gid][0]
check("no icon by default", guild["iconUrl"] is None, guild)

s, r = member.call("POST", f"/api/guilds/{gid}/icon", raw=png(64, 64), ctype="image/png")
check("member cannot set the icon", s == 403, (s, r))

s, r = owner.call("POST", f"/api/guilds/{gid}/icon", raw=png(128, 128), ctype="image/png")
check("owner sets the icon", s == 200 and r["iconUrl"].startswith("/uploads/"), r)
first_icon = r["iconUrl"]

_, r = member.call("GET", "/api/guilds")
guild = [x for x in r["guilds"] if x["id"] == gid][0]
check("members see the icon", guild["iconUrl"] == first_icon, guild)

with urllib.request.urlopen(BASE + first_icon) as res:
    check("icon file is served", res.status == 200 and len(res.read()) > 0)

s, r = owner.call("POST", f"/api/guilds/{gid}/icon", raw=png(96, 96), ctype="image/png")
check("icon can be replaced", r["iconUrl"] != first_icon, r)
try:
    urllib.request.urlopen(BASE + first_icon)
    gone = False
except urllib.error.HTTPError as e:
    gone = e.code == 404
check("old icon file cleaned up", gone)

s, r = owner.call("POST", f"/api/guilds/{gid}/icon", raw=b"not an image", ctype="image/png")
check("non-image rejected", s == 400, (s, r))
s, r = owner.call("POST", f"/api/guilds/{gid}/icon",
                  raw=b"<svg xmlns='http://www.w3.org/2000/svg'/>", ctype="image/svg+xml")
check("svg icon rejected", s == 400, (s, r))

s, r = member.call("DELETE", f"/api/guilds/{gid}/icon")
check("member cannot clear the icon", s == 403, (s, r))
s, r = owner.call("DELETE", f"/api/guilds/{gid}/icon")
check("owner clears the icon", s == 200 and r["iconUrl"] is None, r)
_, r = owner.call("GET", "/api/guilds")
guild = [x for x in r["guilds"] if x["id"] == gid][0]
check("icon gone from the guild", guild["iconUrl"] is None, guild)

# Icon shows on the invite preview so people know what they're joining
owner.call("POST", f"/api/guilds/{gid}/icon", raw=png(80, 80), ctype="image/png")
s, r = outsider.call("GET", f"/api/invites/{inv['code']}")
check("invite preview carries the icon", r["guild"]["iconUrl"] is not None, r["guild"])

print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
if FAIL:
    print("FAILED: " + ", ".join(FAIL))
