#!/usr/bin/env bash
# Deep introspection: is --local actually active, and does the HTTP file
# endpoint work if we give the server time to fetch the file from the DC?
set -uo pipefail
MODE="$1"   # "local" or "normal"
shift
FLAGS=("$@")

T="$TELEGRAM_BOT_TOKEN"
CHAT=6592796294

echo "############################################################"
echo "#  MODE: $MODE   flags: ${FLAGS[*]}"
echo "############################################################"

docker rm -f tapi >/dev/null 2>&1 || true
rm -rf /tmp/tapi && mkdir -p /tmp/tapi && chmod 777 /tmp/tapi

docker run -d --name tapi --network host \
  -v /tmp/tapi:/var/lib/telegram-bot-api \
  -e TELEGRAM_API_ID="$TG_API_ID" -e TELEGRAM_API_HASH="$TG_API_HASH" \
  aiogram/telegram-bot-api:latest "${FLAGS[@]}" >/dev/null

for i in $(seq 1 30); do
  curl -sf --max-time 5 "http://localhost:8081/bot$T/getMe" >/dev/null 2>&1 && break
  sleep 2
done
echo "--- server up ---"

echo "--- docker inspect: what is the container REALLY executing? ---"
docker inspect -f 'ENTRYPOINT={{.Config.Entrypoint}}  CMD={{.Config.Cmd}}' tapi
docker inspect -f 'USER={{.Config.User}}  WORKDIR={{.Config.WorkingDir}}' tapi
docker inspect -f 'MOUNTS={{range .Mounts}}{{.Source}}->{{.Destination}} {{end}}' tapi

echo "--- docker logs (server startup banner shows the effective config) ---"
docker logs tapi 2>&1 | head -30

echo "--- inside the container: what exists under /var/lib/telegram-bot-api? ---"
docker exec tapi sh -c 'ls -la /var/lib/telegram-bot-api 2>&1 | head -20' || true
docker exec tapi sh -c 'find /var/lib/telegram-bot-api -type f 2>/dev/null | head -20' || true

echo "--- build + upload a probe video ---"
ffmpeg -y -hide_banner -loglevel error \
  -f lavfi -i testsrc=size=320x240:rate=15:duration=3 \
  -f lavfi -i sine=frequency=440:duration=3 -shortest \
  -c:v libx264 -preset ultrafast -c:a aac /tmp/probe.mp4
SZ=$(stat -c%s /tmp/probe.mp4)
echo "probe size: $SZ bytes"

RESP=$(curl -s --max-time 120 -F "chat_id=$CHAT" -F "caption=$MODE deep probe" \
  -F "document=@/tmp/probe.mp4;type=video/mp4" \
  "http://localhost:8081/bot$T/sendDocument")
MID=$(echo "$RESP" | python3 -c "import sys,json;print(json.load(sys.stdin)['result']['message_id'])")
FID=$(echo "$RESP" | python3 -c "
import sys,json
r=json.load(sys.stdin)['result']
n=r.get('document') or r.get('video') or {}
print(n.get('file_id',''))")
echo "message_id=$MID  file_id=${FID:0:28}..."

echo "--- getFile: raw response (this is the ground truth) ---"
GF=$(curl -s --max-time 60 "http://localhost:8081/bot$T/getFile?file_id=$FID")
echo "RAW: $GF"
FP=$(echo "$GF" | python3 -c "import sys,json;print(json.load(sys.stdin)['result'].get('file_path',''))")
echo "file_path = '$FP'"

echo "--- regardless of mode: HTTP fetch with RETRIES (server may fetch lazily) ---"
for i in $(seq 1 10); do
  OUT=$(curl -s -o /tmp/got.bin -w "%{http_code}:%{size_download}" --max-time 60 \
        "http://localhost:8081/file/bot$T/$FP")
  echo "  attempt $i -> $OUT"
  case "$OUT" in
    200:*) break ;;
  esac
  sleep 3
done
if [ -f /tmp/got.bin ]; then
  echo "  downloaded bytes: $(stat -c%s /tmp/got.bin)  (uploaded $SZ)"
fi

echo "--- files on the HOST mount after the fetch ---"
find /tmp/tapi -type f -size +1k 2>/dev/null | head -10
echo "--- files INSIDE the container after the fetch ---"
docker exec tapi sh -c 'find /var/lib/telegram-bot-api -type f -size +1k 2>/dev/null | head -10' || true

curl -s --max-time 20 "http://localhost:8081/bot$T/deleteMessage" -d "chat_id=$CHAT" -d "message_id=$MID" >/dev/null
echo "--- done ---"
echo
