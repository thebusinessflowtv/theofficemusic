# Peter Lofi — OVH Streaming Core

This directory is the persistent 24/7 streaming data plane used by MediaForge.

## Current runtime

- `kick`: independent Kick encoder, 1080p30 / CBR 6 Mbps / AAC 160 kbps.
- `twitch`: independent Twitch encoder, 1080p30 / CBR 4.5 Mbps / AAC 160 kbps.
- `youtube-deep-house`: reusable YouTube slot, 1080p60 / CBR 8 Mbps / AAC 192 kbps.
- `youtube-rainy`: reusable YouTube slot, 1080p60 / CBR 8 Mbps / AAC 192 kbps.
- `ovh-agent`: MediaForge command agent. It polls the GitHub control plane and applies start/stop/restart/playlist/track commands locally.
- `control-api`: localhost-only health and command API.
- Docker uses `restart: unless-stopped`, so closing SSH does not stop the streams.

GitHub Actions is no longer the long-running encoder. It is only used for short control-plane tasks such as creating or completing a YouTube broadcast.

## MediaForge control path

```text
MediaForge UI
    ↓
Cloudflare Worker / D1
    ↓
GitHub control/ovh-commands
    ↓
OVH ovh-agent
    ↓
state/<slot>/desired.json + playlist.json
    ↓
stream_core.py / FFmpeg
```

The VPS never exposes a public administrative port. The agent pulls commands from the repository and reports health back to the Cloudflare Worker.

## Runtime state

Each slot keeps isolated state under `state/<slot>/`:

- `desired.json`: desired start/stop state, session ID and visual.
- `playlist.json`: live playlist managed by MediaForge.
- `now-playing.json`: current track.
- `health.json`: encoder health, FPS, bitrate and restart count.
- `command.json`: skip / previous command.
- `ffmpeg.log`: local encoder diagnostics.

## One-command MediaForge rollout

After changes are merged, connect the installed VPS runtime with:

```bash
cd ~/theofficemusic/ovh-streaming
bash connect-mediaforge.sh
```

The script:

1. pulls the current repository;
2. builds the new image before replacing any encoder;
3. starts the MediaForge OVH agent and local control API;
4. rolls Kick, Twitch and both YouTube slots one at a time;
5. waits for each service to report `live`;
6. leaves all containers running under Docker restart policies.

## YouTube slots

The two current YouTube streams are reusable resources. MediaForge creates/completes broadcasts via short YouTube API workflows and binds them to one of these OVH slots. The stream key remains only on the VPS.

To run more than two simultaneous YouTube lives, provision another reusable YouTube stream and another OVH slot.

## Future interactive layer

The runtime is ready for separate platform adapters:

- Twitch chat / Channel Points / Bits.
- Kick chat / rewards.
- Per-platform XP and cooldown policies.
- On-screen chat and now-playing overlay.
- Paid-message event queue.
- Peter Lofi character reactions driven by events.
