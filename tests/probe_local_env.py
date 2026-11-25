#!/usr/bin/env python3
"""Does the file actually land on disk now?

Implements Hermes' documented resolution rule verbatim:
  server reports /var/lib/telegram-bot-api/<TOKEN>/videos/file_0.mp4
  -> read <root>/<TOKEN>/videos/file_0.mp4   (or <root>/videos/... if the token
     dir itself is what's mounted)
"""
import os, subprocess, sys, time, json, urllib.request, urllib.parse

LABEL = sys.argv[1] if len(sys.argv) > 1 else "?"
CHAT = 6592796294
T = os.environ["TELEGRAM_BOT_TOKEN"]
BASE = "http://localhost:8081"
ROOT = "/tmp/tapi"
SERVER_DIR = "/var/lib/telegram-bot-api"


def api(method, params=None, files=None):
    if files:
        boundary = "----p" + os.urandom(8).hex()
        body = bytearray()
        for k, v in (params or {}).items():
            body += ("--%s\r\nContent-Disposition: form-data; name=\"%s\"\r\n\r\n%s\r\n"
                     % (boundary, k, v)).encode()
        for name, (fn, blob, ct) in files.items():
            body += ("--%s\r\nContent-Disposition: form-data; name=\"%s\"; filename=\"%s\"\r\n"
                     "Content-Type: %s\r\n\r\n" % (boundary, name, fn, ct)).encode()
            body += blob + b"\r\n"
        body += ("--%s--\r\n" % boundary).encode()
        req = urllib.request.Request("%s/bot%s/%s" % (BASE, T, method), data=bytes(body),
                                     headers={"Content-Type":
                                              "multipart/form-data; boundary=" + boundary})
    else:
        req = urllib.request.Request("%s/bot%s/%s" % (BASE, T, method),
                                     data=urllib.parse.urlencode(params or {}).encode())
    with urllib.request.urlopen(req, timeout=300) as r:
        return json.loads(r.read())


def listen():
    out = []
    for root, _d, files in os.walk(ROOT):
        for f in files:
            p = os.path.join(root, f)
            try:
                out.append("%s (%d)" % (p, os.path.getsize(p)))
            except OSError:
                pass
    return out


print("#" * 92)
print("#  %s" % LABEL)
print("#" * 92)
print("getMe via local server:", json.dumps(api("getMe").get("result", {}).get("username")))

subprocess.run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                "-f", "lavfi", "-i", "testsrc=size=320x240:rate=15:duration=3",
                "-f", "lavfi", "-i", "sine=frequency=440:duration=3", "-shortest",
                "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac",
                "/tmp/p.mp4"], check=True)
size = os.path.getsize("/tmp/p.mp4")

res = (api("sendDocument", {"chat_id": str(CHAT), "caption": LABEL},
           {"document": ("p.mp4", open("/tmp/p.mp4", "rb").read(), "video/mp4")})
       .get("result") or {})
node = res.get("document") or res.get("video") or {}
mid = res.get("message_id")
print("uploaded %d bytes" % size)

gf = api("getFile", {"file_id": node["file_id"]})
path = (gf.get("result") or {}).get("file_path")
print("file_path: %r  (absolute=%s)" % (path, (path or "").startswith("/")))

# Hermes' rule: strip everything up to and including the token directory,
# then try <root>/<TOKEN>/<rest> and <root>/<rest>.
cands = []
if path:
    rest = path.lstrip("/")
    for pref in (SERVER_DIR.lstrip("/"), ROOT.lstrip("/")):
        if rest.startswith(pref + "/"):
            rest = rest[len(pref) + 1:]
    tok = T.split(":")[0]
    cands = [os.path.join(ROOT, rest), os.path.join(ROOT, tok, rest)]
print("candidates: %s" % cands)

found = None
for i in range(20):
    for c in cands:
        if os.path.isfile(c) and os.path.getsize(c) == size:
            found = c
            break
    if found:
        break
    time.sleep(1.5)

print("files under %s: %s" % (ROOT, listen() or "NONE"))
if found:
    blob = open(found, "rb").read()
    print("VERDICT: ✅ FILE ON DISK after %.1fs at %s | %d bytes | identical=%s"
          % (i * 1.5, found, len(blob), blob == open("/tmp/p.mp4", "rb").read()))
else:
    print("VERDICT: ❌ no file on disk after 30s")

try:
    api("deleteMessage", {"chat_id": str(CHAT), "message_id": str(mid)})
except Exception:
    pass
print()
