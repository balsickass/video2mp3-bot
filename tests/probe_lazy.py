#!/usr/bin/env python3
"""In --local mode, getFile returns an absolute path -- but WHEN does the file
actually appear there? Measure it, and find whatever triggers the fetch.

Phases:
  A. poll the reported path for 60 s after getFile
  B. if still absent, poke the HTTP file endpoint, then poll another 30 s
  C. report the exact triggering action and elapsed time
"""
import os, subprocess, sys, time, json, urllib.request, urllib.parse

CHAT = 6592796294
T = os.environ["TELEGRAM_BOT_TOKEN"]
BASE = "http://localhost:8081"
HOST_DIR = "/tmp/tapi"
CONTAINER_DIR = "/var/lib/telegram-bot-api"


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


def snap():
    out = []
    for root, _d, files in os.walk(HOST_DIR):
        for f in files:
            p = os.path.join(root, f)
            try:
                out.append("%s (%d)" % (p, os.path.getsize(p)))
            except OSError:
                pass
    return out


subprocess.run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                "-f", "lavfi", "-i", "testsrc=size=320x240:rate=15:duration=3",
                "-f", "lavfi", "-i", "sine=frequency=440:duration=3", "-shortest",
                "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac",
                "/tmp/lazy.mp4"], check=True)
size = os.path.getsize("/tmp/lazy.mp4")

res = (api("sendDocument", {"chat_id": str(CHAT), "caption": "lazy-fetch timing"},
           {"document": ("lazy.mp4", open("/tmp/lazy.mp4", "rb").read(), "video/mp4")})
       .get("result") or {})
node = res.get("document") or res.get("video") or {}
mid = res.get("message_id")
print("uploaded %d bytes, message_id=%s" % (size, mid))

t0 = time.time()
gf = api("getFile", {"file_id": node["file_id"]})
path = (gf.get("result") or {}).get("file_path")
print("getFile returned: %r  (t=%.2fs)" % (path, time.time() - t0))
print("size matches upload node: %s" % ((gf.get("result") or {}).get("file_size") == size))

rel = path.lstrip("/")
for b in (CONTAINER_DIR.lstrip("/"), HOST_DIR.lstrip("/")):
    if rel.startswith(b + "/"):
        rel = rel[len(b) + 1:]
        break
host_path = os.path.join(HOST_DIR, rel)
print("expected host path: %s" % host_path)
print("files now: %s" % (snap() or "none"))
print()

# ---- PHASE A: just wait -----------------------------------------------------
print("PHASE A — poll for 60s with no other activity")
found_at = None
for i in range(30):
    time.sleep(2)
    if os.path.isfile(host_path) and os.path.getsize(host_path) == size:
        found_at = time.time() - t0
        print("  ✅ APPEARED after %.1fs (poll %d)" % (found_at, i + 1))
        break
if found_at is None:
    print("  ❌ still absent after 60s")
    print("  files: %s" % (snap() or "none"))

# ---- PHASE B: poke the HTTP endpoint ---------------------------------------
if found_at is None:
    print()
    print("PHASE B — poke the HTTP file endpoint, then poll 30s more")
    url = "%s/file/bot%s/%s" % (BASE, T, path)
    try:
        with urllib.request.urlopen(url, timeout=60) as r:
            blob = r.read()
        print("  HTTP returned %d bytes" % len(blob))
    except Exception as e:
        print("  HTTP -> %s %s" % (type(e).__name__, getattr(e, "code", "")))
    for i in range(15):
        time.sleep(2)
        if os.path.isfile(host_path) and os.path.getsize(host_path) == size:
            found_at = time.time() - t0
            print("  ✅ APPEARED after %.1fs (post-poke poll %d)" % (found_at, i + 1))
            break
    if found_at is None:
        print("  ❌ still absent. files: %s" % (snap() or "none"))

print()
print("VERDICT: %s" % ("✅ lazy fetch confirmed, file lands at %s after %.1fs"
                       % (host_path, found_at) if found_at else
                       "❌ file NEVER appears on disk in local mode"))

try:
    api("deleteMessage", {"chat_id": str(CHAT), "message_id": str(mid)})
except Exception:
    pass
