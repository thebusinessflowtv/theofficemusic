#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"
umask 077

LOOP_URL_DEFAULT="https://mediaforge-api.guilhermeodsgn.workers.dev/media/8d7b81f4-5328-4505-82ac-79cf6221a8e1/51ab646b49d14c55b4720f89f6ee913e1ab5cb5b9df74788afcb099eeb6d6136"

echo
echo "Peter Lofi — OVH setup"
echo "This creates the local .env securely and prepares Docker images."
echo "It will NOT start the live encoders yet, avoiding duplicate publishers."
echo

read -r -p "Kick Stream URL: " KICK_STREAM_URL
read -r -s -p "Kick Stream Key: " KICK_STREAM_KEY
echo
read -r -s -p "NEW Twitch Stream Key: " TWITCH_STREAM_KEY
echo

if [[ -z "$KICK_STREAM_URL" || -z "$KICK_STREAM_KEY" || -z "$TWITCH_STREAM_KEY" ]]; then
  echo "Error: all three values are required." >&2
  exit 1
fi

case "$KICK_STREAM_URL" in
  rtmp://*|rtmps://*) ;;
  *)
    echo "Error: Kick Stream URL must begin with rtmp:// or rtmps://." >&2
    exit 1
    ;;
esac

tmp="$(mktemp)"
trap 'rm -f "$tmp"' EXIT

{
  printf 'KICK_STREAM_URL=%s\n' "$KICK_STREAM_URL"
  printf 'KICK_STREAM_KEY=%s\n' "$KICK_STREAM_KEY"
  printf 'TWITCH_STREAM_KEY=%s\n' "$TWITCH_STREAM_KEY"
  printf 'LOOP_URL=%s\n' "$LOOP_URL_DEFAULT"
} > "$tmp"

install -m 600 "$tmp" .env
mkdir -p state/kick state/twitch

echo
echo "Secure .env created."
echo "Preparing Docker images..."

if docker info >/dev/null 2>&1; then
  docker compose build
else
  sudo docker compose build
fi

echo
echo "READY_FOR_CUTOVER"
echo "Images are built. Kick/Twitch were NOT started yet."
echo "Return to ChatGPT and say: pronto para migrar"
