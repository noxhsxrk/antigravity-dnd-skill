#!/usr/bin/env bash
# stop-ngrok.sh — Stop ngrok tunnel for D&D cinematic display companion

DISPLAY_DIR="$(cd "$(dirname "$0")" && pwd)"
bash "$DISPLAY_DIR/start-ngrok.sh" --stop
