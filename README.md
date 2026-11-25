# video2mp3-bot

Telegram bot **@Vidtoaudioadiosbot** — send it any video, get the audio back as a
downloadable MP3. Hosted free on GitHub Actions as a chained-shift poller.

## What it does

Accepts essentially anything Telegram will hand a bot, strips the audio, encodes
it to MP3, and sends it back:

| Kind | Handling |
|---|---|
| `video` | mp4, mkv, mov, avi, webm, flv, wmv, m4v, 3gp, 3g2, mpg, mpeg, ts, m2ts, vob, ogv, dv, mxf, asf, rm |
| `video_note` | round Telegram video messages (moov-at-END → the hard case) |
| `animation` | animated GIF (silent ones are refused politely) |
| `audio` | any audio format, normalised to MP3 |
| `voice` | Telegram voice notes, normalised to MP3 |
| `document` | anything whose mime/extension looks like video or audio |

Magic-byte sniffing is used, because Telegram's `mime_type` is frequently
missing or wrong.

## File limits — and how they are beaten

Telegram's **public** Bot API caps bot downloads at **20 MB** (`getFile` refuses
larger files server-side and never issues a `file_path`) and uploads at 50 MB.

Because the refusal happens *before any URL exists*, "download the big file in
parts" is **impossible** on the public API — there is nothing to range-request.

**The fix, and what this bot actually runs:** the workflow starts Telegram's own
Bot API server in `--local` mode (Docker, `aiogram/telegram-bot-api`), and the bot
talks to `http://localhost:8081` instead. That raises **both caps to 2000 MB**.

| | public API | self-hosted `--local` |
|---|---|---|
| download | 20 MB | **2000 MB** |
| upload | 50 MB | **2000 MB** |

- `TELEGRAM_API_BASE` selects the endpoint. It **defaults to the public API**, so
  if Docker or the container fails the shift logs `::warning::` and keeps working
  with the smaller ceiling — a smaller ceiling beats a dead bot.
- In `--local` mode `getFile` returns an **absolute local path**; the bot reads it
  off disk and unlinks it so a shift can't fill the runner.
- Needs an `api_id`/`api_hash` pair (secrets `TG_API_ID` / `TG_API_HASH`).
- Readiness is **polled**, never `sleep`-guessed.

MP3s are auto-split on frame boundaries if they somehow exceed the upload cap.


## Design rules (inherited from the voice-stt-bot lineage — do not regress)

1. **Media bytes live in RAM only.** Never written to disk, not even as a temp
   file, so the repo can never leak user media. Not a toggle.
2. **ffmpeg reads from a seekable memfd.** `pipe:0` cannot seek, and any MP4
   whose `moov` atom sits at the **end** (all Telegram video notes, any
   non-faststart upload) demuxes as `partial file` → ffmpeg exits **0** having
   written a **147-byte** ID3-only MP3. `os.memfd_create()` is the fix; `pipe:0`
   stays only as a fallback.
3. **Validate the output, not the exit code.** `_has_mp3_frames()` counts MP3
   sync words (needs ≥3). A non-empty check does *not* catch the 147-byte stub.
4. **`settings.json` lives in the repo.** GHA runners are ephemeral, so a panel
   toggle commits + pushes. State survives restarts.
5. **Chain-dispatch before exit.** Each 45-minute shift dispatches the next one;
   `queued_runs()` counts only `queued` runs (counting the current `in_progress`
   shift made every shift refuse to chain → the bot went dark).
6. **Logs carry metadata only** — sizes, ms, format. Never media, never content.

## Admin panel

`/panel` (admins only: 6592796294, 8439794110) — inline buttons for:

- 🗑️ Delete my original upload — default **OFF** (safe: a failed conversion never
  costs you the source video)
- 🎚️ Quality — high 192k / medium 128k / low 64k
- 📤 Send back as — 🎵 audio player (metadata + inline player) or 📎 document (raw
  file, most download-friendly)

Slash equivalents: `/toggle_delete`, `/quality`, `/toggle_send`.

## Tests

`tests/test_formats.py` builds real media with ffmpeg — including the
**moov-at-END** MP4 — runs the bot's actual `_ffmpeg_mp3()`, and validates output
bytes. Verified result: **23 formats pass**, and the regression test proves the
naive `pipe:0` path loses the audio (147 B) while the memfd path recovers it
(73,498 B of valid 192 kbps audio).

```
python3 tests/test_formats.py
```

## Liveness — read this before probing

**Do NOT check liveness with `getUpdates`.** It is a *destructive* test: a
competing `getUpdates` does not merely report `409 Conflict` — it **terminates the
bot's in-flight long-poll**, which the bot logs as `poll_err` and answers with
exponential backoff. Probing repeatedly kills the thing you're measuring, and the
resulting silence looks like death, which invites more probing.

Measured: 10 probing calls turned into 9x `poll_err HTTPError` in the bot's own
log. The identical shift with **zero** probing logged a clean 45-second empty
long-poll and no errors at all.

Safe signals instead:

1. **Send the bot a video** and watch for the MP3. Nothing beats this.
2. `gh run list -R balsickass/video2mp3-bot` → a run `in_progress`.
3. The job log: `shift start ...` followed by a long clean gap (the empty
   long-poll) is healthy; a wall of `poll_err` is not. Note the log only becomes
   readable **after** the run finishes (`BlobNotFound` while in progress).


## Deploy / restore

Hosted on **`balsickass`** (Burner2) — the disaster-recovery account. To rebuild:

```
gh api -X POST /user/repos -f name=video2mp3-bot -F private=false -F auto_init=false
git remote add origin https://github.com/balsickass/video2mp3-bot.git
git push origin HEAD:main --force
printf '%s' "$TG_TOKEN" | gh secret set TELEGRAM_BOT_TOKEN -R balsickass/video2mp3-bot
gh api -X POST /repos/balsickass/video2mp3-bot/actions/workflows/video2mp3.yml/dispatches -f ref=main
```

Secrets required: `TELEGRAM_BOT_TOKEN` only (no external API — conversion is
local ffmpeg, unlike the voice bot which needed Groq).

## Local mirror

Kept in lockstep at `/storage/emulated/0/HERMES/projects/video2mp3-bot/`
(canonical) and `/storage/emulated/0/HERMES/bots/video2mp3-bot/`.
