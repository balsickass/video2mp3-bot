#!/usr/bin/env python3
"""Head-to-head: can we actually retrieve bytes from a self-hosted Bot API
server in --local mode vs. normal mode?

The 2000 MB file cap comes from running our OWN server, NOT from --local.
--local only changes HOW files move (local filesystem vs HTTP). So the question
is purely: which transfer mechanism gets us the bytes?

Usage: probe_modes.py <label> <base_url>
"""
import importlib.util, json, os, subprocess, sys, urllib.request, urllib.parse

LABEL = sys.argv[1] if len(sys.argv) > 1 else "?"
BASE = sys.argv[2] if len(sys.argv) > 2 else "http://localhost:8081"
CHAT = 6592796294
T = os.environ["TELEGRAM_BOT_TOKEN"]
HOST_DIR = "/tmp/tapi"


def api(method, params=None, files=None, base=None):
    base = base or BASE
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
        req = urllib.request.Request("%s/bot%s/%s" % (base, T, method), data=bytes(body),
                                     headers={"Content-Type":
                                              "multipart/form-data; boundary=" + boundary})
    else:
        req = urllib.request.Request("%s/bot%s/%s" % (base, T, method),
                                     data=urllib.parse.urlencode(params or {}).encode())
    with urllib.request.urlopen(req, timeout=300) as r:
        return json.loads(r.read())


print("=" * 88)
print("  MODE: %s   (base=%s)" % (LABEL, BASE))
print("=" * 88)

SRC = "/tmp/probe_%s.mp4" % LABEL.replace(" ", "_")
subprocess.run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                "-f", "lavfi", "-i", "testsrc=size=320x240:rate=15:duration=3",
                "-f", "lavfi", "-i", "sine=frequency=440:duration=3",
                "-shortest", "-c:v", "libx264", "-preset", "ultrafast",
                "-c:a", "aac", SRC], check=True)
original = open(SRC, "rb").read()
print("  uploaded size: %d bytes" % len(original))

res = (api("sendDocument", {"chat_id": str(CHAT), "caption": "%s probe" % LABEL},
           {"document": ("probe.mp4", original, "video/mp4")}).get("result") or {})
doc = res.get("document") or res.get("video") or {}
mid = res.get("message_id")
if not doc:
    print("  FATAL: no media node"); sys.exit(1)

gf = api("getFile", {"file_id": doc["file_id"]})
fp = (gf.get("result") or {}).get("file_path")
print("  file_path      : %r   (absolute=%s)" % (fp, (fp or "").startswith("/")))

# Where did it land on disk? (local mode writes into --dir)
print("  --- on-disk search under %s ---" % HOST_DIR)
hits = []
for root, dirs, files in os.walk(HOST_DIR):
    for f in files:
        p = os.path.join(root, f)
        sz = os.path.getsize(p)
        if sz > 1000:
            hits.append((p, sz))
print("  files >1KB found: %s" % (hits[:4] if hits else "none"))

got = None
how = []
# Strategy 1: read from disk (local mode), including dir-relative joins
cands = []
if fp:
    if fp.startswith("/"):
        cands.append(fp)
    cands.append(os.path.join(HOST_DIR, fp))  # <host_mount>/<file_path>
    for h, _ in hits:
        if fp and os.path.basename(fp) == os.path.basename(h):
            cands.append(h)
for c in cands:
    if os.path.isfile(c):
        got = open(c, "rb").read()
        how.append("disk:%s" % c)
        break
if got is None and cands:
    how.append("disk-miss(%d tried)" % len(cands))

# Strategy 2: HTTP from the local server
if got is None and fp:
    try:
        with urllib.request.urlopen("%s/file/bot%s/%s" % (BASE, T, fp), timeout=120) as r:
            got = r.read()
        how.append("http-local:%d" % len(got))
    except Exception as e:
        how.append("http-local=%s %s" % (getattr(e, "code", type(e).__name__),
                                         getattr(e, "read", lambda: b"")()
                                         .decode("utf-8", "replace")[:80]
                                         if hasattr(e, "read") else ""))

# Strategy 3: HTTP from the public API
if got is None and fp:
    try:
        with urllib.request.urlopen("https://api.telegram.org/file/bot%s/%s" % (T, fp),
                                    timeout=120) as r:
            got = r.read()
        how.append("http-public:%d" % len(got))
    except Exception as e:
        how.append("http-public=%s" % getattr(e, "code", type(e).__name__))

print("  --- retrieval attempts ---")
for h in how:
    print("    %s" % h)

if got:
    print("  RESULT: ✅ GOT %d bytes, identical=%s" % (len(got), got == original))
    print("  WINNING STRATEGY: %s" % how[-1] if how else "?")
else:
    print("  RESULT: ❌ could not retrieve bytes in this mode")

try:
    api("deleteMessage", {"chat_id": str(CHAT), "message_id": str(mid)})
except Exception:
    pass
print()
