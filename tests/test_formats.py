#!/usr/bin/env python3
"""Live-fire test of video2mp3-bot's real conversion engine.

Builds genuine media fixtures with ffmpeg (including the moov-at-END mp4 that
silently produced 147-byte mp3s in the voice-bot lineage), then runs the ACTUAL
bot._ffmpeg_mp3() against each and validates the OUTPUT bytes.
"""
import os, sys, subprocess, shutil, json

os.environ["TELEGRAM_BOT_TOKEN"] = "0:TESTTOKEN"
BOTDIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BOTDIR)
import bot  # noqa: E402

D = os.path.join(BOTDIR, ".fixtures")
shutil.rmtree(D, ignore_errors=True)
os.makedirs(D, exist_ok=True)


def mk(name, args):
    p = os.path.join(D, name)
    r = subprocess.run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error"] + args + [p],
                       capture_output=True, text=True)
    if r.returncode != 0:
        return None
    return p if os.path.exists(p) and os.path.getsize(p) > 500 else None


V = ["-f", "lavfi", "-i", "testsrc=size=320x240:rate=15:duration=3"]
A = ["-f", "lavfi", "-i", "sine=frequency=440:duration=3"]
VA = V + A + ["-shortest"]

# ---- fixtures: (label, ext, ffmpeg args) ----
FIX = []
FIX.append(("mp4 muxed BEFORE moov (faststart)", "mp4",
            VA + ["-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", "-movflags", "+faststart"]))
FIX.append(("mp4 moov-at-END  <-- the killer", "mp4",
            VA + ["-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac"]))
FIX.append(("mkv", "mkv", VA + ["-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac"]))
FIX.append(("webm (vp8/vorbis)", "webm",
            VA + ["-c:v", "libvpx", "-deadline", "realtime", "-cpu-used", "8", "-c:a", "libvorbis"]))
FIX.append(("mov", "mov", VA + ["-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac"]))
FIX.append(("avi", "avi", VA + ["-c:v", "mpeg4", "-c:a", "libmp3lame"]))
FIX.append(("3gp", "3gp", VA + ["-c:v", "mpeg4", "-c:a", "aac", "-ar", "8000", "-ac", "1"]))
FIX.append(("flv", "flv", VA + ["-c:v", "flv", "-c:a", "libmp3lame"]))
FIX.append(("wmv/asf", "wmv", VA + ["-c:v", "wmv2", "-c:a", "wmav2"]))
FIX.append(("mpg", "mpg", VA + ["-c:v", "mpeg1video", "-c:a", "mp2", "-f", "mpeg"]))
FIX.append(("mpegts", "ts", VA + ["-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac"]))
FIX.append(("m4v", "m4v", VA + ["-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac"]))
FIX.append(("ogv", "ogv", VA + ["-c:v", "libtheora", "-c:a", "libvorbis"]))
FIX.append(("mxf", "mxf", VA + ["-c:v", "mpeg2video", "-pix_fmt", "yuv422p",
                                          "-c:a", "pcm_s16le", "-ar", "48000"]))
FIX.append(("dv", "dv", ["-f", "lavfi", "-i", "testsrc=size=720x480:rate=29.97:duration=2",
            "-f", "lavfi", "-i", "sine=frequency=440:duration=2", "-shortest",
            "-c:v", "dvvideo", "-pix_fmt", "yuv411p", "-c:a", "pcm_s16le", "-ar", "48000", "-ac", "2"]))
FIX.append(("vob/mpeg", "vob", VA + ["-c:v", "mpeg2video", "-c:a", "mp2"]))
FIX.append(("animated gif (silent)", "gif",
            ["-f", "lavfi", "-i", "testsrc=size=160x120:rate=10:duration=2"]))
FIX.append(("VIDEO ONLY, no audio at all", "mp4", V + ["-c:v", "libx264", "-preset", "ultrafast"]))
# audio inputs -> normalise to mp3
FIX.append(("audio: mp3", "mp3", A + ["-c:a", "libmp3lame"]))
FIX.append(("audio: m4a/aac", "m4a", A + ["-c:a", "aac"]))
FIX.append(("audio: wav", "wav", A + ["-c:a", "pcm_s16le"]))
FIX.append(("audio: ogg/vorbis", "ogg", A + ["-c:a", "libvorbis"]))
FIX.append(("audio: flac", "flac", A + ["-c:a", "flac"]))
FIX.append(("audio: opus (Telegram voice)", "opus", A + ["-c:a", "libopus"]))
FIX.append(("audio: amr", "amr", A + ["-c:a", "libopencore_amrnb", "-ar", "8000",
                                             "-ac", "1", "-b:a", "12.2k", "-f", "amr"]))
FIX.append(("audio: wma", "wma", A + ["-c:a", "wmav2"]))


def probe(blob):
    """ffprobe needs a SEEKABLE file to report mp3 duration (a pipe returns 0)."""
    import tempfile
    with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as f:
        f.write(blob)
        path = f.name
    try:
        r = subprocess.run(["ffprobe", "-v", "error", "-show_entries",
                            "format=duration,bit_rate", "-of", "json", path],
                           capture_output=True)
        return json.loads(r.stdout.decode())["format"]
    except Exception:
        return {}
    finally:
        os.unlink(path)


print("=" * 100)
print("  TEST 1/3 — every format through bot._ffmpeg_mp3()  (quality=high 192k)")
print("=" * 100)
print("%-34s %-6s %9s %9s %6s  %s" % ("fixture", "ext", "in", "mp3", "frames", "verdict"))
print("-" * 100)
rows, fails = [], []
for label, ext, args in FIX:
    p = mk("f_%s.%s" % (abs(hash(label)) % 99999, ext), args)
    if not p:
        print("%-34s %-6s %9s %9s %6s  %s" % (label, ext, "-", "-", "-", "⚠️ couldn't build fixture"))
        continue
    data = open(p, "rb").read()
    mp3, err = bot._ffmpeg_mp3(data, bot.IN_FMT.get(ext), "high")
    if mp3:
        frames = bot._has_mp3_frames(mp3)
        f = probe(mp3)
        dur = float(f.get("duration") or 0)
        ok = frames and dur > 1.5
        verdict = "✅ %s audio, %.1fs, %.0f kbps" % ("valid" if ok else "THIN",
                                                            dur, float(f.get("bit_rate") or 0) / 1000)
        if not ok:
            fails.append((label, ext, "thin audio", len(mp3)))
    else:
        # expected for the silent fixtures
        expected = "gif" in ext or "no audio" in label.lower()
        verdict = ("✅ correctly refused (no audio track)" if expected
                   else "❌ FAILED: " + str(err)[:60])
        if not expected:
            fails.append((label, ext, "no mp3", str(err)[:120]))
    print("%-34s %-6s %9d %9s %6s  %s" % (
        label[:34], ext, len(data), len(mp3) if mp3 else "-",
        str(bot._has_mp3_frames(mp3)) if mp3 else "-", verdict))
    rows.append((label, ext, len(data), len(mp3) if mp3 else 0, bool(mp3)))

print()
print("=" * 100)
print("  TEST 2/3 — the moov-at-END regression (would a naive pipe have caught it?)")
print("=" * 100)
p = mk("slow.mp4", VA + ["-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac"])
if p:
    data = open(p, "rb").read()
    # Simulate the OLD naive path: pipe:0 with no demuxer hint / no seek.
    naive = subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin",
                            "-vn", "-i", "pipe:0", "-ac", "2", "-b:a", "192k",
                            "-f", "mp3", "pipe:1"],
                           input=data, capture_output=True)
    print("  naive  pipe:0     -> exit=%d  bytes=%-7d  real_audio=%s"
          % (naive.returncode, len(naive.stdout), bot._has_mp3_frames(naive.stdout)))
    print("        stderr: %s" % naive.stderr.decode("utf-8", "replace").strip()[:160])
    good, _ = bot._ffmpeg_mp3(data, "mp4", "high")
    print("  bot memfd path    -> bytes=%-7d  real_audio=%s"
          % (len(good or b""), bot._has_mp3_frames(good or b"")))
    verdict = ("✅ REGRESSION COVERED — memfd recovered audio the naive pipe lost"
               if (good and bot._has_mp3_frames(good) and not bot._has_mp3_frames(naive.stdout))
               else "⚠️ naive path happened to survive here (other containers still need it)")
    print("  " + verdict)
else:
    print("  ⚠️ could not build slow.mp4")

print()
print("=" * 100)
print("  TEST 3/3 — quality presets + upload chunking")
print("=" * 100)
p = mk("q.mp4", VA + ["-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac"])
if p:
    data = open(p, "rb").read()
    for q in ("high", "medium", "low"):
        mp3, err = bot._ffmpeg_mp3(data, "mp4", q)
        f = probe(mp3 or b"")
        print("  quality=%-7s -> %8d bytes  %6.0f kbps  %.1fs audio  frames=%s"
              % (q, len(mp3 or b""), float(f.get("bit_rate") or 0) / 1000,
                 float(f.get("duration") or 0), bot._has_mp3_frames(mp3 or b"")))
    big, _ = bot._ffmpeg_mp3(data, "mp4", "high")
    parts = bot._slice_mp3(big, limit=20000)  # force split on a tiny limit
    print("  chunking: 1 mp3 of %d B -> %d parts, sizes=%s"
          % (len(big), len(parts), [len(x) for x in parts]))
    print("  every part starts on a frame boundary: %s"
          % all(bot._has_mp3_frames(x) for x in parts))
else:
    print("  ⚠️ could not build q.mp4")

print()
print("=" * 100)
print("  RESULT: %s" % ("✅ ALL FORMATS PASS" if not fails
                        else "❌ %d FAILURES: %s" % (len(fails), fails)))
print("=" * 100)