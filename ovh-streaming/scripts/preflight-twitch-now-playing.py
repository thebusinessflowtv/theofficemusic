#!/usr/bin/env python3
"""Non-disruptive CPU preflight for 1080p Twitch current-song overlay.

Does NOT restart any Docker container, open a live FIFO, or modify desired.json.
Benchmarks a capped, low-priority 8-second FFmpeg encode against the current
local Twitch video cache; exits nonzero if insufficient headroom.
"""
from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys
import time

ROOT=pathlib.Path("/home/ubuntu/theofficemusic/ovh-streaming/state")
VISUAL=ROOT/"twitch"/"visual-health.json"
HEALTH=ROOT/"twitch"/"health.json"
NOW=ROOT/"twitch"/"now-playing.json"
TITLE=ROOT/"twitch"/"now-playing-overlay.txt"
CACHE=pathlib.Path("/state/twitch/visual-cache")
FONT="/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
CONTAINER="peter-lofi-twitch"


def read(path):
    try:
        result=json.loads(path.read_text(encoding="utf-8"))
        return result if isinstance(result,dict) else {}
    except (OSError,ValueError):
        return {}


def mem_available():
    for line in pathlib.Path("/proc/meminfo").read_text().splitlines():
        if line.startswith("MemAvailable:"):
            return int(line.split()[1])*1024
    return 0


def main():
    if os.geteuid()!=0:raise RuntimeError("Run with sudo on OVH")
    health=read(HEALTH)
    visual=read(VISUAL)
    song=read(NOW)
    if health.get("status")!="live" or visual.get("status")!="streaming":
        raise RuntimeError("Twitch not confirmed live: benchmark skipped")
    source=pathlib.PurePosixPath(str(visual.get("prepared_file") or ""))
    if source==pathlib.PurePosixPath(".") or source.parent!=CACHE:
        raise RuntimeError("Not a trusted local Twitch video cache file")
    if mem_available()<2.5*1024**3:
        raise RuntimeError("Less than 2.5 GiB RAM available; do not enable overlay")
    if os.getloadavg()[0]>2.5:
        raise RuntimeError("CPU already busy; postpone overlay benchmark")
    title=" ".join(str(song.get("title") or "PeterLofi Radio").split())
    artists=" ".join(str(song.get("artists") or "").split())
    label=title if not artists else title+" - "+artists
    if len(label)>45:label=label[:42].rstrip()+"..."
    tmp=TITLE.with_suffix(".txt.preflight")
    tmp.write_text(label+"\n",encoding="utf-8")
    try:
        # Exact VisualEngine overlay filter with short text-file path.
        filter=(
            "drawbox=x=32:y=ih-132:w=920:h=99:color=black@0.52:t=fill,"
            f"drawtext=fontfile={FONT}:text='NOW PLAYING':"
            "fontcolor=white@0.8:fontsize=23:x=56:y=h-120,"
            f"drawtext=fontfile={FONT}:textfile=/state/twitch/{tmp.name}:"
            "fontcolor=white:fontsize=34:x=56:y=h-82,"
            "drawbox=x=iw-680:y=ih-113:w=648:h=80:color=black@0.52:t=fill,"
            f"drawtext=fontfile={FONT}:"
            "text='!skip    !song    !back    !freeze':"
            "fontcolor=white:fontsize=30:x=w-text_w-55:y=h-86"
        )
        cmd=[
            "docker","exec",CONTAINER,
            "nice","-n","15","ffmpeg","-hide_banner","-nostats",
            "-loglevel","error","-nostdin",
            "-i",str(source),"-t","8","-map","0:v:0","-an",
            "-vf",filter,"-c:v","libx264","-preset","superfast",
            "-threads","2","-bf","0","-pix_fmt","yuv420p",
            "-f","null","-"
        ]
        print("Preflight: 8 seconds at 1080p with nice=15 and 2 encoding threads",flush=True)
        start=time.monotonic()
        out=subprocess.run(cmd,capture_output=True,text=True,timeout=65)
        elapsed=time.monotonic()-start
        if out.returncode:
            # Truncate errors; no credentials or stream URLs are involved.
            raise RuntimeError("FFmpeg local overlay failed: "+out.stderr[-300:])
        speed=8/elapsed
        print(f"Local overlay encoder speed: {speed:.2f}x real-time",flush=True)
        print(f"Available RAM: {mem_available()/1024**3:.1f} GiB",flush=True)
        if speed<1.3:
            raise RuntimeError("Encoder has insufficient speed margin for stable 30fps; keep overlay disabled")
        print("OVERLAY_PREFLIGHT_PASSED: rendering appears feasible.",flush=True)
        print("This is only a test; Twitch RTMP encoder, container, and sender were NOT restarted.",flush=True)
        print("Activation requires a separate, explicitly approved maintenance operation.",flush=True)
    finally:
        tmp.unlink(missing_ok=True)


if __name__=="__main__":
    try:
        main()
    except (RuntimeError, subprocess.TimeoutExpired, OSError) as exc:
        print("OVERLAY_PREFLIGHT_FAILED:",str(exc),file=sys.stderr)
        sys.exit(1)
