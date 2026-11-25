#!/usr/bin/env python3
"""Empirically determine how a containerised --local Bot API server reports
file paths, and verify bot.fetch_media() can actually retrieve the bytes.

Round-trip: generate a real mp4 -> upload it -> getFile -> run the SHIPPED
fetch_media() against it -> compare bytes.

Deliberately never calls getUpdates: that would kill the live shift's in-flight
long-poll and poison the very thing under test.
"""
import importlib.util, json, os, subprocess, sys, urllib.request, urllib.parse

CHAT = 6592796294
T = os.environ["TELEGRAM_BOT_TOKEN"]
BASE = os.environ.get("TELEGRAM_API_BASE", "http://localhost:8081")
LOCAL_DIR = "/var/lib/telegram-bot-api"   # --dir INSIDE the container
HOST_DIR = "/tmp/tapi"                    # where it's mounted on the runner


def api(method, params=None, files=None):
    if files:
        boundary = "----probe" + os.urandom(8).hex()
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
    with urllib.request.urlopen(req, timeout=180) as r:
        return json.loads(r.read())


print("=" * 88)
print("  STEP 1 — build a real mp4 (with audio) to upload")
print("=" * 88)
SRC = "/tmp/probe_src.mp4"
subprocess.run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                "-f", "lavfi", "-i", "testsrc=size=320x240:rate=15:duration=3",
                "-f", "lavfi", "-i", "sine=frequency=440:duration=3",
                "-shortest", "-c:v", "libx264", "-preset", "ultrafast",
                "-c:a", "aac", SRC], check=True)
original = open(SRC, "rb").read()
print("  built %s (%d bytes)" % (SRC, len(original)))

print()
print("=" * 88)
print("  STEP 2 — upload it through the LOCAL server, capture the document node")
print("=" * 88)
sent = api("sendDocument", {"chat_id": str(CHAT), "caption": "download-path probe"},
           {"document": ("probe.mp4", original, "video/mp4")})
ok = sent.get("ok")
doc = (sent.get("result") or {}).get("document") or {}
mid = (sent.get("result") or {}).get("message_id")
print("  sendDocument ok=%s  file_id=%s…" % (ok, str(doc.get("file_id"))[:28]))
print("  message_id=%s  (will be deleted at the end)" % mid)
if not doc:
    print("  FATAL: no document returned"); print(json.dumps(sent)[:600]); sys.exit(1)

print()
print("=" * 88)
print("  STEP 3 — what does getFile actually return?")
print("=" * 88)
gf = api("getFile", {"file_id": doc["file_id"]})
fp = (gf.get("result") or {}).get("file_path")
print("  raw getFile response: %s" % json.dumps(gf)[:300])
print("  file_path            : %r" % fp)
print("  is absolute          : %s" % (fp or "").startswith("/"))
if fp and fp.startswith(LOCAL_DIR):
    rel = fp[len(LOCAL_DIR):].lstrip("/")
    translated = os.path.join(HOST_DIR, rel)
else:
    translated = None
print("  exists AT REPORTED path (host view) : %s" % os.path.exists(fp or ""))
print("  translated -> %s" % translated)
print("  exists TRANSLATED (%s) : %s" % (HOST_DIR, os.path.exists(translated) if translated else "n/a"))
if translated and os.path.isdir(os.path.dirname(translated)):
    print("  dir listing: %s" % os.listdir(os.path.dirname(translated))[:5])
print("  dir exists at all?      : %s" % os.path.isdir(os.path.dirname(fp or "")))
print("  host mount listing (/tmp/tapi): %s" % (os.listdir(HOST_DIR)[:6]
                                               if os.path.isdir(HOST_DIR) else "MISSING"))
print("  HTTP via local server   : ", end="")
try:
    with urllib.request.urlopen("%s/file/bot%s/%s" % (BASE, T, fp), timeout=30) as r:
        print("HTTP %s, %d bytes" % (r.status, len(r.read())))
except Exception as e:
    code = getattr(e, "code", None)
    body = ""
    try:
        body = e.read().decode("utf-8", "replace")[:120]
    except Exception:
        pass
    print("%s %s %s" % (type(e).__name__, code or "", body))

print()
print("=" * 88)
print("  STEP 4 — the SHIPPED bot.fetch_media() against this exact document")
print("=" * 88)
os.environ["TELEGRAM_API_BASE"] = BASE
spec = importlib.util.spec_from_file_location("bt", "bot.py")
m = importlib.util.module_from_spec(spec)
sys.modules["bt"] = m
spec.loader.exec_module(m)
print("  bot sees LOCAL_API=%s  LOCAL_API_DIR=%r  HOST_DIR=%r"
      % (m.LOCAL_API, m.LOCAL_API_DIR, m.LOCAL_API_HOST_DIR))

fake_msg = {"chat": {"id": CHAT}, "from": {"id": CHAT}, "message_id": mid,
            "document": doc}
data, ext, node, kind = m.fetch_media(fake_msg)
if data:
    same = data == original
    print("  ✅ fetch_media OK: %d bytes, ext=%r, kind=%r, identical_to_upload=%s"
          % (len(data), ext, kind, same))
    print("  VERDICT: %s" % ("✅ DOWNLOAD PATH WORKS" if same else
                             "⚠️ bytes differ (still retrieved)"))
else:
    print("  ❌ fetch_media FAILED, reason = %s" % ext)
    print("  VERDICT: ❌ DOWNLOAD PATH STILL BROKEN")

print()
print("=" * 88)
print("  STEP 5 — clean up the probe message")
print("=" * 88)
try:
    d = api("deleteMessage", {"chat_id": str(CHAT), "message_id": str(mid)})
    print("  deleteMessage ok=%s" % d.get("ok"))
except Exception as e:
    print("  delete failed: %s" % e)
