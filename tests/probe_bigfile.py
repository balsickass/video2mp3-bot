#!/usr/bin/env python3
"""The only question that matters: can we pull a >20 MB file, and where do the
bytes end up?

Behaviour observed so far:
  * non-local server  -> file DOES appear at <dir>/<TOKEN>/<file_path>
  * --local server    -> absolute path returned, but file NEVER appears

So test a 30 MB payload through both modes and see which one actually delivers.
"""
import os, subprocess, sys, time, json, urllib.request, urllib.parse

LABEL = sys.argv[1]
CHAT = 6592796294
T = os.environ["TELEGRAM_BOT_TOKEN"]
BASE = "http://localhost:8081"
ROOT = "/tmp/tapi"
SERVER_DIR = "/var/lib/telegram-bot-api"
SRC = "/tmp/big30.mp4"


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
    with urllib.request.urlopen(req, timeout=600) as r:
        return json.loads(r.read())


def listen():
    out = []
    for root, _d, files in os.walk(ROOT):
        for f in files:
            p = os.path.join(root, f)
            try:
                out.append("%s (%d)" % (p.replace(ROOT, "~"), os.path.getsize(p)))
            except OSError:
                pass
    return out


print("#" * 92)
print("#  %s" % LABEL)
print("#" * 92)

if not os.path.exists(SRC):
    # ~30 MB: clearly over the 20 MB public-API ceiling.
    subprocess.run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                    "-f", "lavfi", "-i", "testsrc=size=1280x720:rate=30:duration=12",
                    "-c:v", "libx264", "-preset", "ultrafast", "-b:v", "20M",
                    "-f", "lavfi", "-i", "sine=frequency=440:duration=12",
                    "-shortest", "-c:a", "aac", SRC], check=True)
size = os.path.getsize(SRC)
print("payload: %d bytes (%.1f MB)  -> %s the 20 MB ceiling"
      % (size, size / 1e6, "OVER" if size > 20e6 else "under"))

try:
    res = (api("sendDocument", {"chat_id": str(CHAT), "caption": LABEL},
               {"document": ("big30.mp4", open(SRC, "rb").read(), "video/mp4")})
           .get("result") or {})
except Exception as e:
    body = ""
    try:
        body = e.read().decode("utf-8", "replace")[:200]
    except Exception:
        pass
    print("UPLOAD FAILED: %s %s %s" % (type(e).__name__, getattr(e, "code", ""), body))
    sys.exit(0)

node = res.get("document") or res.get("video") or {}
mid = res.get("message_id")
print("upload OK, message_id=%s" % mid)

gf = api("getFile", {"file_id": node["file_id"]})
path = (gf.get("result") or {}).get("file_path")
print("getFile ok=%s file_path=%r absolute=%s"
      % (gf.get("ok"), path, (path or "").startswith("/")))
if not gf.get("ok"):
    print("GETFILE REJECTED:", json.dumps(gf)[:300])

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

print("files under mount: %s" % (listen() or "NONE"))

# Also try the HTTP endpoint as a fallback path.
http_ok = None
if path:
    try:
        with urllib.request.urlopen("%s/file/bot%s/%s" % (BASE, T, path), timeout=300) as r:
            blob = r.read()
        http_ok = len(blob)
    except Exception as e:
        http_ok = "%s %s" % (type(e).__name__, getattr(e, "code", ""))
print("http /file/ -> %s" % http_ok)

if found:
    blob = open(found, "rb").read()
    print("VERDICT: ✅ DISK DELIVERY WORKS — %s | %d bytes | identical=%s"
          % (found, len(blob), blob == open(SRC, "rb").read()))
elif isinstance(http_ok, int) and http_ok == size:
    print("VERDICT: ✅ HTTP DELIVERY WORKS — %d bytes" % http_ok)
elif gf.get("ok") and path:
    print("VERDICT: ⚠️ getFile accepted a >20MB file but NO delivery method returned it")
else:
    print("VERDICT: ❌ getFile refused the file (cap still enforced)")

try:
    api("deleteMessage", {"chat_id": str(CHAT), "message_id": str(mid)})
except Exception:
    pass
print()