#!/usr/bin/env python3
"""video2mp3-bot: Telegram video/audio -> MP3, sent back for download.

Bot: @Vidtoaudioadiosbot (id 9288741292)
Accept ANY media Telegram will hand us, convert the audio track to MP3, send
the MP3 back as a downloadable file.

HARD RULES (inherited from the voice-stt-bot lineage -- do not regress):
  * media bytes live in RAM only - never written to disk. memfd or pipe:0.
  * ffmpeg MUST read from a SEEKABLE source. pipe:0 cannot seek, and any MP4
    whose `moov` atom sits at the END (Telegram video notes, non-faststart
    uploads) demuxes as "partial file" -> ffmpeg exits 0 having written a
    silent 147-byte ID3-only mp3. memfd_create() is the fix.
  * validate the OUTPUT, not just the exit code: count mp3 sync words.
  * logs carry METADATA only (sizes/ms/format) - never media bytes.
  * settings.json lives IN THE REPO: a toggle commits+pushes so state survives
    ephemeral GHA runners.
  * chain-dispatch the next shift BEFORE exiting so coverage never gaps.
"""
import json, os, shutil, subprocess, time, urllib.request, urllib.parse, urllib.error

TG_BOT = os.environ["TELEGRAM_BOT_TOKEN"]
# Optional self-hosted Bot API server. Point TELEGRAM_API_BASE at a locally-run
# telegram-bot-api (--local) and the FILE CAPS CHANGE:
#     20 MB download  ->  2000 MB
#     50 MB upload    ->  2000 MB
# That is the only way to beat the 20 MB getFile limit -- the public API refuses
# larger files outright and never hands out a file_path, so "download it in
# parts" is impossible there (there is no URL to range-request).
API_BASE = os.environ.get("TELEGRAM_API_BASE", "https://api.telegram.org").rstrip("/")
LOCAL_API = API_BASE != "https://api.telegram.org"
TG = API_BASE + "/bot" + TG_BOT
LOCAL_API_DIR = os.environ.get("TELEGRAM_LOCAL_DIR", "/var/lib/telegram-bot-api")
RUN_MIN = float(os.environ.get("RUN_MINUTES", "45"))
START = time.time()
DEADLINE = START + RUN_MIN * 60
ADMINS = (6592796294, 8439794110)
SETTINGS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "settings.json")
DEFAULT_SETTINGS = {"delete_original": False, "quality": "high", "send_mode": "audio"}

# Telegram file ceilings. With a self-hosted --local server these jump to 2000 MB.
TG_MAX_DL = (2000 if LOCAL_API else 20) * 1024 * 1024
TG_MAX_UP = (2000 if LOCAL_API else 50) * 1024 * 1024
UPLOAD_HEADROOM = (1900 if LOCAL_API else 48) * 1024 * 1024

QUALITY = {
    "high":   ["-ac", "2", "-ar", "44100", "-b:a", "192k"],
    "medium": ["-ac", "2", "-ar", "44100", "-b:a", "128k"],
    "low":    ["-ac", "1", "-ar", "22050", "-b:a", "64k"],
}

# Containers that need an explicit demuxer hint when fed via a descriptor/memfd.
IN_FMT = {"aac": "aac", "amr": "amr", "wma": "asf", "aiff": "aiff",
          "ape": "ape", "tta": "tta", "mka": "matroska", "mkv": "matroska",
          "mov": "mov", "avi": "avi", "mpg": "mpeg", "mpeg": "mpeg",
          "3gp": "3gp", "3g2": "3gp", "caf": "caf", "au": "au", "snd": "au",
          "ac3": "ac3", "dts": "dts", "midi": "midi", "ra": "rm", "rm": "rm",
          "weba": "webm", "flv": "flv", "wmv": "asf", "ts": "mpegts",
          "m2ts": "mpegts", "vob": "mpeg", "ogv": "ogg", "dv": "dv"}

VIDEO_EXTS = {"mp4", "mkv", "mov", "avi", "webm", "flv", "wmv", "m4v", "3gp",
              "3g2", "mpg", "mpeg", "ts", "m2ts", "vob", "ogv", "mts", "dv",
              "asf", "rm", "rmvb", "f4v", "m2v", "mxf", "gif", "m3u8"}
AUDIO_EXTS = {"mp3", "wav", "m4a", "aac", "ogg", "oga", "opus", "flac", "wma",
              "amr", "aiff", "aif", "mka", "ape", "caf", "au", "ac3", "dts",
              "mid", "midi", "ra", "weba", "wv", "tta", "spx", "gsm"}


def log(*a):
    print("[%6.1fs] " % (time.time() - START) + " ".join(str(x) for x in a), flush=True)


def tg(method, params=None, timeout=120):
    data = urllib.parse.urlencode(params).encode() if params else None
    req = urllib.request.Request(TG + "/" + method, data=data)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


# ---------------- settings: load / save / persist to repo ----------------

def load_settings():
    try:
        with open(SETTINGS_FILE, encoding="utf-8") as f:
            return {**DEFAULT_SETTINGS, **json.load(f)}
    except Exception as e:
        log("settings_load_fallback", type(e).__name__)
        return dict(DEFAULT_SETTINGS)


def git(args):
    env = dict(os.environ)
    env["GIT_TERMINAL_PROMPT"] = "0"
    for k, v in (("GIT_AUTHOR_NAME", "video2mp3-bot"), ("GIT_AUTHOR_EMAIL", "v2m@local"),
                 ("GIT_COMMITTER_NAME", "video2mp3-bot"), ("GIT_COMMITTER_EMAIL", "v2m@local")):
        env.setdefault(k, v)
    return subprocess.run(["git"] + args, capture_output=True, text=True, env=env)


def persist_settings(settings):
    """Commit settings.json back to the repo. GHA runners are ephemeral, so the
    repo IS the persistence layer. Never fatal -- a failed push must not take the
    shift down."""
    try:
        with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
            json.dump(settings, f, indent=2, sort_keys=True)
        git(["config", "user.name", "video2mp3-bot"])
        git(["config", "user.email", "v2m@local"])
        if git(["add", "settings.json"]).returncode != 0:
            return False
        if git(["commit", "-m", "settings: panel toggles"]).returncode != 0:
            return True  # nothing changed
        git(["pull", "--rebase", "-q", "origin", os.environ.get("SETTINGS_BRANCH", "main")])
        p = git(["push", "-q", "origin", "HEAD:" + os.environ.get("SETTINGS_BRANCH", "main")])
        log("settings_persist", "ok" if p.returncode == 0 else "push_fail")
        return p.returncode == 0
    except Exception as e:
        log("settings_persist_err", type(e).__name__)
        return False


# ---------------- media plumbing ----------------

def _doc_is_media(doc):
    """True when a Telegram `document` should be treated as convertible media."""
    mime = (doc.get("mime_type") or "").lower()
    ext = os.path.splitext((doc.get("file_name") or "").lower())[1].lstrip(".")
    if mime.startswith(("video/", "audio/")):
        return True
    return ext in VIDEO_EXTS or ext in AUDIO_EXTS


def _pick_node(m):
    """Media node to convert. Prefer real video, then video-note, animation,
    audio, then a video/audio document. Returns (node, kind)."""
    for key in ("video", "video_note", "animation", "audio", "voice"):
        node = m.get(key)
        if node:
            return node, key
    doc = m.get("document")
    if doc and _doc_is_media(doc):
        return doc, "document"
    return None, None


def oversized(node):
    """True when Telegram's Bot API will refuse to hand us the file at all."""
    return int(node.get("file_size") or 0) > TG_MAX_DL


def _sniff_ext(blob, fallback="bin"):
    """Magic-byte sniff -- Telegram's mime_type is often missing or lying."""
    h = blob[:16]
    if h[4:8] == b"ftyp":
        return "mp4"
    if h[:4] == b"\x1a\x45\xdf\xa3":
        return "webm"
    if h[:4] == b"RIFF":
        if blob[8:12] == b"WAVE":
            return "wav"
        if blob[8:12] == b"AVI ":
            return "avi"
        return "webp"
    if h[:4] == b"OggS":
        return "ogg"
    if h[:3] == b"ID3" or h[:2] in (b"\xff\xfb", b"\xff\xf3", b"\xff\xf2", b"\xff\xfa"):
        return "mp3"
    if h[:4] == b"fLaC":
        return "flac"
    if h[:6] == b"#!AMR\x0a":
        return "amr"
    if h[:4] == b"FORM":
        return "aiff"
    if h[:4] == b"\x30\x26\xb2\x75":
        return "wmv"
    if h[:2] == b"\x1f\x8b":
        return "gz"
    if h[:4] == b"FLV\x01":
        return "flv"
    if h[:4] == b"\x00\x00\x01\xba" or h[:4] == b"\x00\x00\x01\xb3":
        return "mpg"
    return fallback


def _has_mp3_frames(blob):
    """True when the blob really carries audio: >=3 mp3 sync words.

    A failed/partial demux still exits 0 and emits a 147-byte ID3-only stub.
    A non-empty check does NOT catch that -- frame count does."""
    n, i = 0, blob.find(b"\xff")
    while 0 <= i < len(blob) - 1:
        if blob[i + 1] & 0xE0 == 0xE0:
            n += 1
            if n >= 3:
                return True
        i = blob.find(b"\xff", i + 1)
    return False


def _ffmpeg_mp3(data, in_fmt=None, quality="high"):
    """bytes -> mp3 bytes. RAM only. Returns (mp3_bytes, stderr_text)."""
    if not shutil.which("ffmpeg"):
        return None, "ffmpeg-not-installed"
    hdr = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin"]
    enc = ["-vn", "-map", "0:a:0"] + QUALITY.get(quality, QUALITY["high"]) + \
          ["-f", "mp3", "pipe:1"]
    last_err = ""

    def attempt(cmd, fd):
        nonlocal last_err
        try:
            p = subprocess.run(cmd, capture_output=True, timeout=900,
                               pass_fds=(fd,) if fd is not None else ())
            err = p.stderr.decode("utf-8", "replace").strip()
            last_err = err[:300]
            if p.returncode == 0 and _has_mp3_frames(p.stdout):
                return p.stdout
            if p.returncode == 0:
                log("ffmpeg_empty_output", len(p.stdout), err[:120])
            else:
                log("ffmpeg_exit", p.returncode, err[:120])
        except Exception as e:
            last_err = type(e).__name__
            log("ffmpeg_err", type(e).__name__)
        finally:
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass
        return None

    # 1) seekable anonymous RAM descriptor (the moov-at-end fix)
    if hasattr(os, "memfd_create"):
        try:
            fd = os.memfd_create("v2m_media", 0)
            os.write(fd, data)
            os.lseek(fd, 0, os.SEEK_SET)
            src = (["-f", in_fmt] if in_fmt else []) + ["-i", "/proc/self/fd/%d" % fd]
            got = attempt(hdr + src + enc, fd)
            if got:
                return got, ""
        except OSError:
            pass

    # 2) pipe fallback -- fine for faststart / self-describing containers
    src = (["-f", in_fmt] if in_fmt else []) + ["-i", "pipe:0"]
    return attempt(hdr + src + enc, None), last_err


def _slice_mp3(blob, limit=None):
    """Cut an mp3 byte stream into <limit parts on mp3 frame boundaries so no
    part starts mid-frame. Needed only for very long videos."""
    limit = limit or UPLOAD_HEADROOM
    if len(blob) <= limit:
        return [blob]
    starts, i = [], blob.find(b"\xff")
    while 0 <= i < len(blob) - 1:
        if blob[i + 1] & 0xE0 == 0xE0:
            starts.append(i)
        i = blob.find(b"\xff", i + 1)
    if not starts:
        return [blob]
    tail_min = min(40 * 1024, max(1, limit // 4))
    parts, cur = [], 0
    while cur < len(blob):
        best = -1
        for s in starts:
            if s <= cur:
                continue
            if s - cur > limit:
                break
            if len(blob) - s < tail_min:
                continue
            best = s
        if best < 0:
            break
        parts.append(blob[cur:best])
        cur = best
    rest = blob[cur:]
    if parts and len(rest) < tail_min:
        parts[-1] = parts[-1] + rest
    elif rest:
        parts.append(rest)
    return [p for p in parts if p]


def _no_audio_hint(err):
    e = (err or "").lower()
    return ("does not contain any stream" in e or "output file #0 does not contain" in e
            or "stream map" in e or "matches no streams" in e
            or "no audio" in e or "does not contain stream" in e)


# ---------------- control panel ----------------

def _yn(v):
    return "🟢 ON" if v else "🔴 OFF"


def panel_text():
    q = (SETTINGS.get("quality") or "high").upper()
    mode = "🎵 Audio player" if SETTINGS.get("send_mode", "audio") == "audio" else "📎 Document (raw file)"
    return ("🎬➡️🎵 Video → MP3 Panel\n\n"
            "🗑️ Delete my original upload: %s\n"
            "🎚️ Quality: %s  (high 192k / medium 128k / low 64k)\n"
            "📤 Send back as: %s\n"
            "💾 Local media files: 🛡️ NEVER written (RAM only, not a toggle)\n\n"
            "Send ANY video or audio: mp4 mkv mov avi webm flv wmv 3gp mpg ts m2ts\n"
            "vob ogv dv m4v mxf asf rm + audio formats + video notes + MP3s\n"
            "(audio is normalised too). Animated GIFs are converted to audio.\n\n"
            "📥 I can take videos up to %d MB\n"
            "📤 MP3s come back up to %d MB (auto-split on frame boundaries if longer)\n\n"
            "Settings survive restarts (pushed to the repo)."
            % (_yn(SETTINGS.get("delete_original")), q, mode,
               TG_MAX_DL // 1048576, TG_MAX_UP // 1048576))


def panel_kb():
    q = (SETTINGS.get("quality") or "high").upper()
    mode = "🎵 audio" if SETTINGS.get("send_mode", "audio") == "audio" else "📎 document"
    return [
        [("🗑️ Delete original: " + _yn(SETTINGS.get("delete_original")), "toggle_del")],
        [("🎚️ Quality: " + q, "cycle_q")],
        [("📤 Send as: " + mode, "toggle_send")],
        [("💾 Local: NEVER written", "locked_info"), ("🔄 Refresh", "refresh_panel")],
    ]


def kb_markup(rows):
    return json.dumps({"inline_keyboard": [
        [{"text": t, "callback_data": d} for t, d in row] for row in rows]})


def send_panel(chat, reply_to=None):
    params = {"chat_id": chat, "text": panel_text(), "reply_markup": kb_markup(panel_kb())}
    if reply_to:
        params["reply_to_message_id"] = reply_to
    return tg("sendMessage", params)


def refresh_panel(chat, mid):
    try:
        tg("editMessageText", {"chat_id": chat, "message_id": mid,
                               "text": panel_text(), "reply_markup": kb_markup(panel_kb())})
        return True
    except Exception as e:
        log("panel_edit_fail", chat, type(e).__name__)
        return False


def is_admin(uid):
    return uid in ADMINS


def _admin_only(chat):
    try:
        tg("sendMessage", {"chat_id": chat, "text": "admin only 😘"})
    except Exception:
        pass


def handle_callback(q):
    uid = (q.get("from") or {}).get("id", 0)
    data = q.get("data") or ""
    msg = q.get("message") or {}
    chat = msg.get("chat", {}).get("id")
    mid = msg.get("message_id")
    if not is_admin(uid):
        try:
            tg("answerCallbackQuery", {"callback_query_id": q.get("id"),
                                       "text": "admin only", "show_alert": True})
        except Exception:
            pass
        log("panel_denied", uid)
        return
    try:
        tg("answerCallbackQuery", {"callback_query_id": q.get("id")})
    except Exception:
        pass
    if data == "toggle_del":
        SETTINGS["delete_original"] = not SETTINGS.get("delete_original", False)
        persist_settings(SETTINGS)
    elif data == "cycle_q":
        order = ["high", "medium", "low"]
        cur = SETTINGS.get("quality", "high")
        SETTINGS["quality"] = order[(order.index(cur) + 1) % len(order)] if cur in order else "high"
        persist_settings(SETTINGS)
    elif data == "toggle_send":
        SETTINGS["send_mode"] = "document" if SETTINGS.get("send_mode", "audio") == "audio" else "audio"
        persist_settings(SETTINGS)
    elif data == "locked_info":
        try:
            tg("answerCallbackQuery", {"callback_query_id": q.get("id"),
                                       "text": "Media lives in RAM only — never on disk.",
                                       "show_alert": True})
        except Exception:
            pass
        return
    refresh_panel(chat, mid)
    log("panel", data, JSONDUMP())


def JSONDUMP():
    return json.dumps({k: SETTINGS.get(k) for k in sorted(DEFAULT_SETTINGS)})


# ---------------- file upload (multipart) ----------------
# The voice bot only ever SENT TEXT, so it never needed this. This bot must
# upload an mp3, which requires multipart/form-data -- urlencode cannot do it.

import uuid


def _multipart(fields, files):
    boundary = "----v2mbot" + uuid.uuid4().hex
    out = bytearray()
    for k, v in fields.items():
        out += ("--%s\r\nContent-Disposition: form-data; name=\"%s\"\r\n\r\n%s\r\n"
                % (boundary, k, v)).encode("utf-8")
    for name, (fname, data, ctype) in files.items():
        out += ("--%s\r\nContent-Disposition: form-data; name=\"%s\"; filename=\"%s\"\r\n"
                "Content-Type: %s\r\n\r\n" % (boundary, name, fname, ctype)).encode("utf-8")
        out += data + b"\r\n"
    out += ("--%s--\r\n" % boundary).encode("utf-8")
    return bytes(out), "multipart/form-data; boundary=" + boundary


def tg_send_file(method, fields, files, timeout=600):
    body, ctype = _multipart(fields, files)
    req = urllib.request.Request(TG + "/" + method, data=body,
                                 headers={"Content-Type": ctype,
                                          "Content-Length": str(len(body))})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def _safe_name(name, default="audio.mp3"):
    n = (name or "").strip().replace("/", "_").replace("\\", "_")
    n = "".join(c for c in n if c.isprintable() and c not in "\r\n\t")
    if not n:
        return default
    if len(n) > 120:
        root, ext = os.path.splitext(n)
        n = root[:110] + ext
    return n


def _mp3_name(node, kind):
    """Build a sensible .mp3 filename from whatever Telegram gave us."""
    base = (node.get("file_name") or "").strip()
    if not base:
        base = {"video_note": "video_note", "animation": "animation",
                "video": "video", "audio": "audio", "voice": "voice"}.get(kind, "media")
    root = os.path.splitext(base)[0] or "media"
    return _safe_name(root + ".mp3")


def _title(node, kind):
    for k in ("title", "file_name"):
        v = (node.get(k) or "").strip()
        if v:
            return os.path.splitext(v)[0][:60]
    return {"video_note": "Video note", "animation": "Animation",
            "video": "Video", "audio": "Audio", "voice": "Voice"}.get(kind, "Media")


# ---------------- download ----------------

def fetch_media(m):
    """Download the message's media into RAM. Returns (data, ext, node, kind)
    or (None, reason, node, kind)."""
    node, kind = _pick_node(m)
    if not node:
        return None, "no-media", None, None
    info = tg("getFile", {"file_id": node["file_id"]}, timeout=60)
    if not info.get("ok"):
        return None, "getfile-rejected", node, kind
    path = info["result"]["file_path"]

    # Self-hosted --local server: file_path is an ABSOLUTE LOCAL PATH and the
    # file can be far bigger than 20 MB. Read it straight off disk.
    if LOCAL_API and os.path.isabs(path) and os.path.exists(path):
        with open(path, "rb") as f:
            data = f.read()
        try:
            os.unlink(path)  # don't fill the runner's disk across the shift
        except OSError:
            pass
    else:
        url = "%s/file/bot%s/%s" % (API_BASE, TG_BOT, path)
        with urllib.request.urlopen(url, timeout=1800) as r:
            data = r.read()

    mime = (node.get("mime_type") or "").lower()
    ext = ""
    for k in ("video/", "audio/", "image/"):
        if mime.startswith(k):
            ext = mime.split("/", 1)[1].split(";")[0].strip()
            break
    if not ext:
        ext = os.path.splitext(path)[1].lstrip(".").lower()
    if not ext:
        ext = os.path.splitext((node.get("file_name") or ""))[1].lstrip(".").lower()
    sniffed = _sniff_ext(data, "")
    # Trust magic bytes when they disagree with an empty/generic mime label.
    if sniffed and (not ext or ext in ("bin", "octet-stream", "unknown")):
        ext = sniffed
    if not ext:
        ext = sniffed or "bin"
    return data, ext, node, kind


# ---------------- core conversion + delivery ----------------

def convert_and_send(m):
    chat = m["chat"]["id"]
    mid = m.get("message_id")
    uid = (m.get("from") or {}).get("id", 0)
    t0 = time.time()

    node, kind = _pick_node(m)
    if node is None:
        return  # text/photo/sticker: ignore silently

    if oversized(node):
        mb = int(node.get("file_size") or 0) / 1048576.0
        cap_mb = TG_MAX_DL // 1048576
        try:
            tg("sendMessage", {
                "chat_id": chat, "reply_to_message_id": mid,
                "text": ("⚠️ that file is %.1f MB — over my %d MB ceiling.\n\n"
                         "send me it under %d MB and I'll convert it." % (mb, cap_mb, cap_mb))})
        except Exception:
            pass
        log("too_big", kind, int(node.get("file_size") or 0))
        return

    data, ext, node, kind = None, None, node, kind
    reason = None
    for attempt, delay in enumerate((0, 2, 5)):
        if delay:
            time.sleep(delay)
        try:
            data, ext, node, kind = fetch_media(m)
            if data:
                reason = None
                break
            reason = ext  # fetch_media reuses the 2nd slot for the failure reason
        except Exception as e:
            reason = type(e).__name__
            log("dl_fail", chat, reason, "try", attempt + 1)
    if not data:
        try:
            tg("sendMessage", {"chat_id": chat, "reply_to_message_id": mid,
                               "text": "couldn't pull that file from Telegram (%s) — resend it 🙏"
                                       % (reason or "unknown")})
        except Exception:
            pass
        log("dl_fail_gave_up", chat, reason)
        return

    size_mb = len(data) / 1048576.0
    quality = SETTINGS.get("quality", "high")
    qlabel = {"high": "192k", "medium": "128k", "low": "64k"}.get(quality, "192k")
    log("got", kind, ext, len(data), "q=" + str(quality))

    # Tell him we're working -- a 20 MB video is not instant on a shared runner.
    note = None
    try:
        note = tg("sendMessage", {
            "chat_id": chat, "reply_to_message_id": mid,
            "text": "🎬 got it — %.1f MB %s\n⏳ converting to MP3 (%s)…" % (size_mb, ext.upper(), qlabel)})
    except Exception:
        pass
    note_id = ((note or {}).get("result") or {}).get("message_id")

    mp3, err = _ffmpeg_mp3(data, IN_FMT.get(ext), quality)
    del data  # RAM only — hard rule, no toggle
    if not mp3:
        msg = ("❌ ffmpeg couldn't decode that %s file.\n\n`%s`"
               % (ext.upper(), (err or "unknown error")[:200]))
        if _no_audio_hint(err):
            msg = ("🔇 that %s has **no audio track** (or only video) — nothing to convert."
                   % ext.upper())
        try:
            if note_id:
                tg("editMessageText", {"chat_id": chat, "message_id": note_id, "text": msg})
            else:
                tg("sendMessage", {"chat_id": chat, "text": msg, "reply_to_message_id": mid})
        except Exception:
            pass
        log("convert_fail", chat, kind, ext, str(err)[:150])
        return

    fname = _mp3_name(node, kind)
    title = _title(node, kind)
    mode = SETTINGS.get("send_mode", "audio")
    chunks = _slice_mp3(mp3)
    sent = 0
    for i, blob in enumerate(chunks, 1):
        part = " (%d/%d)" % (i, len(chunks)) if len(chunks) > 1 else ""
        out_name = fname if len(chunks) == 1 else fname[:-4] + "_part%d.mp3" % i
        fields = {"chat_id": str(chat)}
        if mid and i == 1:
            fields["reply_to_message_id"] = str(mid)
        try:
            if mode == "document":
                fields["document"] = "x"
                tg_send_file("sendDocument", {k: v for k, v in fields.items() if k != "document"},
                             {"document": (out_name, blob, "audio/mpeg")})
            else:
                fields["title"] = title + part
                fields["performer"] = "video2mp3"
                tg_send_file("sendAudio", fields,
                             {"audio": (out_name, blob, "audio/mpeg")})
            sent += 1
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "replace")[:200]
            log("send_fail", chat, e.code, body)
            # 413/400 on size: retry once as a lower-bitrate mp3
            try:
                tg("sendMessage", {"chat_id": chat,
                                   "text": "⚠️ Telegram refused the upload:\n`%s`" % body})
            except Exception:
                pass
        except Exception as e:
            log("send_err", chat, type(e).__name__)

    if note_id and sent:
        gb = len(mp3) / 1048576.0
        try:
            tg("editMessageText", {
                "chat_id": chat, "message_id": note_id,
                "text": "✅ done — %.1f MB MP3 (%s)%s\nfrom %.1f MB %s · %.1fs"
                        % (gb, qlabel, " · %d parts" % len(chunks) if len(chunks) > 1 else "",
                           size_mb, ext.upper(), time.time() - t0)})
        except Exception:
            pass

    deleted = "off"
    if SETTINGS.get("delete_original", False) and sent:
        try:
            d = tg("deleteMessage", {"chat_id": chat, "message_id": mid})
            deleted = str(bool(d.get("ok")))
        except Exception:
            deleted = "fail"
    log("done", chat, kind, ext, "in=%d" % len(mp3), "sent=%d" % sent,
        "%dms" % int((time.time() - t0) * 1000), "del=" + str(deleted), "by=" + str(uid))


# ---------------- commands ----------------

HELP = ("🎬➡️🎵 **Video → MP3**\n\n"
        "Send me ANY video and I'll return the audio as an MP3 you can download.\n\n"
        "**Accepts:** mp4 mkv mov avi webm flv wmv m4v 3gp 3g2 mpg mpeg ts m2ts "
        "vob ogv dv mxf asf rm — plus video notes, animated GIFs, and every audio "
        "format (also normalised to MP3).\n\n"
        "📥 Videos up to **%d MB** in. MP3s up to **%d MB** back out "
        "(auto-split if longer).\n\n"
        "Nothing is ever written to disk — media lives in RAM only.")


def help_text():
    return HELP % (TG_MAX_DL // 1048576, TG_MAX_UP // 1048576)


def handle(m):
    chat = (m.get("chat") or {}).get("id")
    uid = (m.get("from") or {}).get("id", 0)
    mid = m.get("message_id")
    txt = (m.get("text") or "").strip()
    low = txt.lower()

    if low.startswith("/panel"):
        if not is_admin(uid):
            _admin_only(chat)
            return
        send_panel(chat, mid)
        log("panel", "opened", uid)
        return
    if low.startswith("/toggle_delete"):
        if not is_admin(uid):
            _admin_only(chat)
            return
        SETTINGS["delete_original"] = not SETTINGS.get("delete_original", False)
        persist_settings(SETTINGS)
        tg("sendMessage", {"chat_id": chat, "text": "Delete original upload: "
                           + _yn(SETTINGS["delete_original"])})
        return
    if low.startswith("/toggle_send"):
        if not is_admin(uid):
            _admin_only(chat)
            return
        SETTINGS["send_mode"] = "document" if SETTINGS.get("send_mode", "audio") == "audio" else "audio"
        persist_settings(SETTINGS)
        tg("sendMessage", {"chat_id": chat, "text": "Send back as: " + SETTINGS["send_mode"]})
        return
    if low.startswith("/quality"):
        if not is_admin(uid):
            _admin_only(chat)
            return
        order = ["high", "medium", "low"]
        cur = SETTINGS.get("quality", "high")
        SETTINGS["quality"] = order[(order.index(cur) + 1) % len(order)] if cur in order else "high"
        persist_settings(SETTINGS)
        tg("sendMessage", {"chat_id": chat, "text": "Quality: " + SETTINGS["quality"].upper()})
        return
    if low.startswith(("/start", "/help")):
        extra = "\n\nAdmin: /panel for toggles." if is_admin(uid) else ""
        tg("sendMessage", {"chat_id": chat, "text": help_text() + extra,
                           "parse_mode": "Markdown",
                           **({"reply_to_message_id": mid} if mid else {})})
        return
    if txt.startswith("/"):
        return  # unknown command: stay quiet
    convert_and_send(m)


# ---------------- shift chaining ----------------

def queued_runs(token, repo, wf):
    """Count only QUEUED runs. The current shift is itself `in_progress`, and
    counting that made every shift refuse to chain -> the bot went dark."""
    url = ("https://api.github.com/repos/%s/actions/workflows/%s/runs?per_page=5"
           % (repo, wf))
    req = urllib.request.Request(url, headers={
        "Authorization": "Bearer " + token,
        "Accept": "application/vnd.github+json", "User-Agent": "v2m-chain"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            runs = json.loads(r.read()).get("workflow_runs", [])
        me = os.environ.get("GITHUB_RUN_ID", "")
        return sum(1 for x in runs if x.get("status") == "queued" and str(x.get("id")) != me)
    except Exception as e:
        log("chain_queue_probe_fail", type(e).__name__)
        return 0


def dispatch_next():
    token = os.environ.get("GITHUB_TOKEN", "")
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    wf = os.environ.get("WORKFLOW_FILE", "video2mp3.yml")
    if not (token and repo):
        log("chain", "no-token")
        return
    already = queued_runs(token, repo, wf)
    if already:
        log("chain", "skipped-queued-runs", already)
        return
    req = urllib.request.Request(
        "https://api.github.com/repos/%s/actions/workflows/%s/dispatches" % (repo, wf),
        data=json.dumps({"ref": "main"}).encode(),
        headers={"Authorization": "Bearer " + token,
                 "Accept": "application/vnd.github+json", "User-Agent": "v2m-chain"})
    try:
        urllib.request.urlopen(req, timeout=30)
        log("chain", "next-shift-dispatched")
    except Exception as e:
        log("chain", "FAIL", type(e).__name__)


# ---------------- main loop ----------------

def main():
    global SETTINGS
    SETTINGS = load_settings()
    try:
        tg("deleteWebhook", {"drop_pending_updates": False})
    except Exception as e:
        log("webhook_clear_fail", type(e).__name__)
    offset = 0
    chained = False
    polls = {}
    log("shift start", "budget", RUN_MIN, "min", "quality", SETTINGS.get("quality"),
        "send_mode", SETTINGS.get("send_mode"),
        "delete_original", SETTINGS.get("delete_original"),
        "ffmpeg", bool(shutil.which("ffmpeg")))
    while time.time() < DEADLINE:
        if not chained and time.time() > DEADLINE - 45:
            dispatch_next()
            chained = True
        try:
            res = tg("getUpdates", {"offset": offset, "timeout": 45,
                                    "allowed_updates": json.dumps(["message", "callback_query"])},
                     timeout=70)
        except Exception as e:
            # 409 = another poller on the same token; 5xx/timeouts = flaky runner
            # net. NEVER log just the class name -- an HTTPError's meaning is
            # entirely in its status code and body, and a bare "HTTPError" sent
            # me chasing a phantom for 20 minutes. Log the code and the body.
            err = type(e).__name__
            detail = ""
            try:
                if isinstance(e, urllib.error.HTTPError):
                    detail = "HTTP %s %s" % (e.code, e.read().decode("utf-8", "replace")[:300])
                else:
                    detail = ("%s: %s" % (type(e).__name__, e))[:300]
            except Exception:
                detail = "(detail unavailable)"
            polls[err] = polls.get(err, 0) + 1
            log("poll_err", detail, polls[err])
            time.sleep(min(2 + polls[err] * 3, 45))
            continue
        polls.clear()
        for upd in res.get("result", []):
            offset = upd["update_id"] + 1
            try:
                if upd.get("callback_query"):
                    handle_callback(upd["callback_query"])
                else:
                    handle(upd.get("message") or {})
            except Exception as e:
                log("handle_err", type(e).__name__)
    if not chained:
        dispatch_next()
    log("shift end - no media persisted")


SETTINGS = DEFAULT_SETTINGS

if __name__ == "__main__":
    main()
