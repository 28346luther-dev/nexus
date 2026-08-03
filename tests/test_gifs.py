"""Tests for the Giphy proxy and GIF favourites.

The proxy tests need GIPHY_API_KEY set on the server under test; they are
skipped with a clear note when it isn't, so the favourites half still runs.
"""
import json
import sys
import urllib.error
import urllib.request

BASE = "http://127.0.0.1:" + (sys.argv[1] if len(sys.argv) > 1 else "8130")
PASS, FAIL, SKIP = [], [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f'{"PASS" if cond else "FAIL"}  {name}' + (f"   {detail}" if detail and not cond else ""))


def skip(name, why):
    SKIP.append(name)
    print(f"SKIP  {name}   ({why})")


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


a = C()
a.call("POST", "/api/register",
       {"username": "Giffer", "email": "gif1@x.com", "password": "hunter2hunter2"})
other = C()
other.call("POST", "/api/register",
           {"username": "Nother", "email": "gif2@x.com", "password": "hunter2hunter2"})

s, r = C().call("GET", "/api/gifs?q=cat")
check("gif search needs an account", s == 401, s)
s, r = C().call("GET", "/api/gifs/favourites")
check("favourites need an account", s == 401, s)

# ------------------------------------------------------------- giphy proxy
s, r = a.call("GET", "/api/gifs?q=cat")
live = s == 200 and r.get("gifs")
sample = None
if live:
    sample = r["gifs"][0]
    check("search returns results", len(r["gifs"]) > 0)
    check("results carry the fields the picker needs",
          all(k in sample for k in ("id", "preview", "url", "full", "title")), sample)
    check("urls are https giphy",
          sample["full"].startswith("https://") and "giphy.com" in sample["full"],
          sample["full"])
    s2, r2 = a.call("GET", "/api/gifs")
    check("trending works with no query", s2 == 200 and len(r2["gifs"]) > 0)
    s3, _ = a.call("GET", "/api/gifs?q=" + "x" * 300)
    check("an overlong query is handled", s3 in (200, 502), s3)
else:
    why = (r or {}).get("error", "no response")
    skip("giphy proxy", why)

# --------------------------------------------------------------- favourites
gid = sample["id"] if sample else "test-gif-1"
url = sample["full"] if sample else "https://media.giphy.com/media/test/giphy.gif"
preview = sample["preview"] if sample else url

s, f = a.call("GET", "/api/gifs/favourites")
check("favourites start empty", s == 200 and f["gifs"] == [], f)

s, f = a.call("POST", "/api/gifs/favourites",
              {"id": gid, "url": url, "preview": preview, "title": "a gif"})
check("star saves a gif", s == 200 and f["favourite"] is True, f)

s, f = a.call("GET", "/api/gifs/favourites")
check("saved gif is listed", len(f["gifs"]) == 1 and f["gifs"][0]["id"] == gid, f)
check("listed gif is flagged as a favourite", f["gifs"][0]["favourite"] is True, f["gifs"][0])
check("listed gif keeps its url", f["gifs"][0]["full"] == url, f["gifs"][0])

s, f = a.call("POST", "/api/gifs/favourites", {"id": gid, "url": url})
check("starring again un-saves it", f["favourite"] is False, f)
s, f = a.call("GET", "/api/gifs/favourites")
check("un-saved gif is gone", f["gifs"] == [], f)

s, f = a.call("POST", "/api/gifs/favourites",
              {"id": "evil", "url": "http://evil.example/x.gif"})
check("non-https url rejected", s == 400, (s, f))
s, f = a.call("POST", "/api/gifs/favourites", {"url": url})
check("missing id rejected", s == 400, (s, f))
s, f = a.call("POST", "/api/gifs/favourites", {"id": "noturl"})
check("missing url rejected", s == 400, (s, f))

a.call("POST", "/api/gifs/favourites", {"id": "mine-1", "url": url})
s, f = other.call("GET", "/api/gifs/favourites")
check("favourites are private to each account", f["gifs"] == [], f)

print(f"\n{len(PASS)} passed, {len(FAIL)} failed, {len(SKIP)} skipped")
if FAIL:
    print("FAILED: " + ", ".join(FAIL))
