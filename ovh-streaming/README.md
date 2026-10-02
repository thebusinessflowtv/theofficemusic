# Peter Lofi — OVH Streaming Core

This directory is the persistent 24/7 streaming data plane for Peter Lofi.

## Why it exists

GitHub Actions remains useful as a control plane, but hosted runners have a hard lifetime ceiling. Kick and Twitch therefore move to OVH, where FFmpeg can stay connected indefinitely and Docker restarts failed processes automatically.

## Architecture

- `kick`: independent Kick encoder, 1080p60 / CBR 8 Mbps / AAC 160 kbps.
- `twitch`: independent Twitch encoder, 1080p30 / CBR 4.5 Mbps / AAC 160 kbps.
- `audio_engine.py`: track-by-track playback, shuffle, now-playing state and commands.
- `control-api`: local API for MediaForge/bots.
- `state/<platform>/now-playing.json`: current track.
- `state/<platform>/health.json`: encoder health.
- `state/<platform>/command.json`: latest skip/previous command.

Kick stays Kick and Twitch stays Twitch. They share music assets, but each platform has its own encoder process, bitrate profile, state and command channel.

## First deployment

1. Copy `.env.example` to `.env` and set the real stream secrets.
2. Keep the server firewall closed except for SSH. The control API binds to localhost only.
3. Run:
   ```bash
   docker compose up -d --build
   docker compose ps
   curl http://127.0.0.1:8787/health
   ```

## Control examples

Skip the current Twitch track:

```bash
curl -X POST http://127.0.0.1:8787/command/twitch \
  -H 'Content-Type: application/json' \
  -d '{"action":"skip","source":"manual"}'
```

Go back one track on Kick:

```bash
curl -X POST http://127.0.0.1:8787/command/kick \
  -H 'Content-Type: application/json' \
  -d '{"action":"previous","source":"manual"}'
```

## Next adapters

The control API is intentionally platform-neutral. The next layer will add separate adapters for:

- Twitch chat / Channel Points / Bits.
- Kick chat / platform rewards.
- Per-platform XP and cooldown policies.
- On-screen chat and now-playing overlay.
- Paid-message event queue.
- Peter Lofi character reactions driven by events.

The current Gaming station starts with the existing 3-hour source as one track. As the 5-minute library is generated, `stations/gaming.json` can be replaced with individual tracks without changing the encoder layer.
