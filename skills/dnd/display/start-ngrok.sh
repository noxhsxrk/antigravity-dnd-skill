#!/usr/bin/env bash
# start-ngrok.sh — Start ngrok tunnel for D&D cinematic display companion (port 5001)
#
# Usage:
#   bash start-ngrok.sh
#   bash start-ngrok.sh --stop

DISPLAY_DIR="$(cd "$(dirname "$0")" && pwd)"
PID_FILE="$DISPLAY_DIR/ngrok.pid"
LOG_FILE="$DISPLAY_DIR/ngrok.log"
URL_FILE="$DISPLAY_DIR/.ngrok_url"
PORT=5001

# Find ngrok executable
NGROK_BIN="$(command -v ngrok || echo "/opt/homebrew/bin/ngrok")"

if [[ ! -x "$NGROK_BIN" ]]; then
  echo "Error: ngrok is not installed or not in PATH."
  echo "Please install ngrok via: brew install ngrok"
  exit 1
fi

# Stop mode
if [[ "$1" == "--stop" ]]; then
  if [[ -f "$PID_FILE" ]]; then
    kill "$(cat "$PID_FILE")" 2>/dev/null || true
    rm -f "$PID_FILE"
  fi
  pkill -f "ngrok http $PORT" 2>/dev/null || true
  rm -f "$URL_FILE"
  echo "ngrok tunnel stopped."
  exit 0
fi

# Clean up existing instance
if [[ -f "$PID_FILE" ]]; then
  kill "$(cat "$PID_FILE")" 2>/dev/null || true
  rm -f "$PID_FILE"
fi
pkill -f "ngrok http $PORT" 2>/dev/null || true
rm -f "$URL_FILE"
sleep 0.5

# Check if display server is running on port 5001
if ! curl -sk "http://localhost:$PORT/ping" > /dev/null 2>&1; then
  echo "Notice: D&D display server is not running on port $PORT yet."
  echo "Starting display companion first..."
  bash "$DISPLAY_DIR/start-display.sh" > /dev/null 2>&1 &
  sleep 1.5
fi

# Start ngrok
echo "Starting ngrok tunnel for http://localhost:$PORT..."
nohup "$NGROK_BIN" http "$PORT" --log=stdout > "$LOG_FILE" 2>&1 &
echo $! > "$PID_FILE"

# Wait for ngrok API to become available and fetch public URL
PUBLIC_URL=""
for i in $(seq 1 15); do
  sleep 0.5
  PUBLIC_URL=$(curl -s http://127.0.0.1:4040/api/tunnels 2>/dev/null | python3 -c '
import sys, json
try:
    data = json.load(sys.stdin)
    tunnels = data.get("tunnels", [])
    https_tunnels = [t["public_url"] for t in tunnels if t.get("public_url", "").startswith("https://")]
    if https_tunnels:
        print(https_tunnels[0])
    elif tunnels:
        print(tunnels[0]["public_url"])
except Exception:
    pass
')
  if [[ -n "$PUBLIC_URL" ]]; then
    break
  fi
done

if [[ -n "$PUBLIC_URL" ]]; then
  echo "$PUBLIC_URL" > "$URL_FILE"
  echo ""
  echo "=============================================================="
  echo "  D&D Display Companion is now online via ngrok!"
  echo "=============================================================="
  echo "  Public URL:    $PUBLIC_URL"
  echo "  Local URL:     http://localhost:$PORT"
  echo "  ngrok Web UI:  http://127.0.0.1:4040"
  echo "=============================================================="
  echo "Share the Public URL with players on phones, tablets, or remote screens."
  echo "To stop ngrok:   bash $DISPLAY_DIR/stop-ngrok.sh"
  echo "=============================================================="
  echo ""
  exit 0
else
  echo "Warning: ngrok started but could not retrieve public URL from http://127.0.0.1:4040/api/tunnels."
  echo "Check log file for errors: $LOG_FILE"
  exit 1
fi
