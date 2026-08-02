"""Tests for /purge and custom statuses."""
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
owner.call("POST", "/api/register", {"username": "Owner", "email": "eo@x.com", "password": "hunter2hunter2"})
member.call("POST", "/api/register", {"username": "Member", "email": "em@x.com", "password": "hunter2hunter2"})
outsider.call("POST", "/api/register", {"username": "Outsider", "email": "eu@x.com", "password": "hunter2hunter2"})

_, g = owner.call("POST", "/api/guilds", {"name": "Mods"})
gid, cid = g["guildId"], g["channelId"]
_, inv = owner.call("POST", f"/api/guilds/{gid}/invites", {})
member.call("POST", f"/api/invites/{inv['code']}/join")

# -------------------------------------------------------------------- purge
for i in range(12):
    owner.call("POST", f"/api/channels/{cid}/messages", {"content": f"message {i}"})
_, up = owner.call("POST", f"/api/channels/{cid}/upload?filename=x.png",
                   raw=png(20, 20), ctype="image/png")
img_url = up["message"]["attachments"][0]["url"]

_, r = owner.call("GET", f"/api/channels/{cid}/messages")
before = len(r["messages"])
check("channel has messages to purge", before == 13, before)

s, r = member.call("POST", f"/api/channels/{cid}/purge", {"count": 5})
check("non-owner cannot purge", s == 403, (s, r))
s, r = outsider.call("POST", f"/api/channels/{cid}/purge", {"count": 5})
check("non-member cannot purge", s == 403, (s, r))

s, r = owner.call("POST", f"/api/channels/{cid}/purge", {"count": 0})
check("zero rejected", s == 400, (s, r))
s, r = owner.call("POST", f"/api/channels/{cid}/purge", {"count": -3})
check("negative rejected", s == 400, (s, r))
s, r = owner.call("POST", f"/api/channels/{cid}/purge", {"count": 500})
check("over the cap rejected", s == 400, (s, r))
s, r = owner.call("POST", f"/api/channels/{cid}/purge", {"count": "lots"})
check("non-numeric rejected", s == 400, (s, r))

# The newest message is the image; purging 1 should take it and its file.
s, r = owner.call("POST", f"/api/channels/{cid}/purge", {"count": 1})
check("owner purges", s == 200 and r["deleted"] == 1, (s, r))
try:
    urllib.request.urlopen(BASE + img_url)
    gone = False
except urllib.error.HTTPError as e:
    gone = e.code == 404
check("purged image file deleted from disk", gone)

s, r = owner.call("POST", f"/api/channels/{cid}/purge", {"count": 5})
check("purge deletes exactly N", r["deleted"] == 5, r)
_, r = owner.call("GET", f"/api/channels/{cid}/messages")
check("only the newest were removed", [m["content"] for m in r["messages"]]
      == [f"message {i}" for i in range(7)], [m["content"] for m in r["messages"]])

# Asking for more than exist deletes what there is, without error
s, r = owner.call("POST", f"/api/channels/{cid}/purge", {"count": 50})
check("purging more than exist is fine", s == 200 and r["deleted"] == 7, (s, r))
_, r = owner.call("GET", f"/api/channels/{cid}/messages")
check("channel now empty", len(r["messages"]) == 0, r)

# Not allowed in DMs
_, me_m = member.call("GET", "/api/me")
_, dm = owner.call("POST", "/api/dms", {"userId": me_m["user"]["id"]})
owner.call("POST", f"/api/channels/{dm['channelId']}/messages", {"content": "hi"})
s, r = owner.call("POST", f"/api/channels/{dm['channelId']}/purge", {"count": 1})
check("purge refused in a dm", s == 400, (s, r))

# ------------------------------------------------------------------ status
s, r = owner.call("PATCH", "/api/me", {"status": "Building the site"})
check("set status", s == 200 and r["user"]["status"] == "Building the site", r)
s, r = member.call("GET", f"/api/guilds/{gid}/members")
row = [m for m in r["members"] if m["username"] == "Owner"][0]
check("status visible to others in member list", row["status"] == "Building the site", row)
s, r = member.call("GET", f'/api/users/{row["id"]}')
check("status on the profile", r["user"]["status"] == "Building the site", r["user"])

s, r = owner.call("PATCH", "/api/me", {"status": "x" * 200})
check("overlong status trimmed to 60", len(r["user"]["status"]) == 60, len(r["user"]["status"]))
s, r = owner.call("PATCH", "/api/me", {"status": "  lots\n\n of   space  "})
check("whitespace collapsed", r["user"]["status"] == "lots of space", repr(r["user"]["status"]))
s, r = owner.call("PATCH", "/api/me", {"status": ""})
check("status can be cleared", r["user"]["status"] == "", repr(r["user"]["status"]))

s, r = owner.call("PATCH", "/api/me", {"status": "<script>alert(1)</script>"})
check("status stored verbatim (escaped at render)",
      r["user"]["status"] == "<script>alert(1)</script>", r["user"]["status"])

# Defaults
s, r = outsider.call("GET", "/api/me")
check("status defaults to empty", r["user"]["status"] == "", repr(r["user"]["status"]))

print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
if FAIL:
    print("FAILED: " + ", ".join(FAIL))
