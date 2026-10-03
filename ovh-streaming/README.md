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


## Remote deploy agent

The VPS also runs a host-level systemd service named `mediaforge-deploy-agent`. It is separate from the streaming `ovh-agent`.

Purpose:
- pull the authoritative `main` branch;
- build and recreate one allow-listed Docker Compose service;
- perform rolling full-runtime deploys;
- report health;
- perform a service rollback to the previously recorded Git revision.

Security model:
- no arbitrary shell commands are accepted;
- deploy actions and targets are allow-listed both by Cloudflare and by the host agent;
- the agent polls Cloudflare D1 outbound, so no public SSH/admin port is added;
- GitHub remains the source of truth;
- deployment results are acknowledged back to MediaForge.

Allowed actions:
- `deploy_service`: one of `ovh-agent`, `control-api`, `kick`, `twitch`, `youtube-deep-house`, `youtube-rainy`;
- `deploy_all`: rolling update, one service at a time;
- `deploy_host_agent`: update/reload only the host deployment agent;
- `health_check`: sanitized health for all services;
- `rollback_service`: rebuild one service from the previous recorded Git revision.

One-time installation:

```bash
cd ~/theofficemusic/ovh-streaming
git pull --ff-only
sudo bash install-host-deploy-agent.sh
```

After `MEDIAFORGE_REMOTE_DEPLOY_READY`, normal code deployments no longer require an interactive SSH session.
