#!/usr/bin/env python3
"""Kick visual-only CPU preflight with Twitch online. No live changes."""
from __future__ import annotations
import json
import os
from pathlib import Path, PurePosixPath
import subprocess
import sys
import time

ROOT=Path("/home/ubuntu/theofficemusic/ovh-streaming/state")
KICK=ROOT/"kick"
CONTAINER="peter-lofi-kick"
FONT="/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
LOCK_FONT="/usr/share/fonts/truetype/ancient-scripts/Symbola_hint.ttf"


def read(path):
    try:
        data=json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data,dict) else {}
    except (ValueError,OSError):
        return {}


def mem_available():
    for line in Path("/proc/meminfo").read_text().splitlines():
        if line.startswith("MemAvailable:"):
            return int(line.split()[1])*1024
    return 0


def sample_cpu_idle(seconds=2.0):
    """Sample actual host CPU idle, not Linux runnable-task load average.

    A 1-minute load >2.5 can coexist with ~50% idle on four vCores.
    The benchmark still measures real FFmpeg throughput under live load.
    """
    def counters():
        fields=Path("/proc/stat").read_text().splitlines()[0].split()[1:]
        parts=[int(field) for field in fields]
        return sum(parts), parts[3], parts[4], parts[7] if len(parts)>7 else 0
    a=counters()
    time.sleep(seconds)
    b=counters()
    total=max(1,b[0]-a[0])
    return ((b[1]-a[1])/total,
            (b[2]-a[2])/total,
            (b[3]-a[3])/total)


def main():
    if os.geteuid()!=0:
        raise RuntimeError("Run with sudo on the OVH host.")
    h=read(KICK/"health.json")
    v=read(KICK/"visual-health.json")
    twitch=read(ROOT/"twitch"/"health.json")
    if h.get("status")!="live" or v.get("status")!="streaming":
        raise RuntimeError("Kick is not confirmed live; leaving all streams untouched.")
    if twitch.get("status")!="live":
        raise RuntimeError("Twitch must be live for a realistic dual-live performance test.")
    source=PurePosixPath(str(v.get("prepared_file") or ""))
    if source.parent!=PurePosixPath("/state/kick/visual-cache") or not source.name:
        raise RuntimeError("Kick prepared_file must be a verified local cache MP4.")
    if mem_available()<3.0*1024**3:
        raise RuntimeError("Insufficient available RAM (need >=3 GiB) for dual overlay encoding.")
    idle,iowait,steal=sample_cpu_idle()
    print(f"HOST_LOAD_1M: {os.getloadavg()[0]:.2f}",flush=True)
    print(f"HOST_IDLE_SAMPLE: {idle*100:.1f}%",flush=True)
    print(f"HOST_IOWAIT_SAMPLE: {iowait*100:.1f}%",flush=True)
    print(f"HOST_STEAL_SAMPLE: {steal*100:.1f}%",flush=True)
    if idle<0.35:
        raise RuntimeError(
            "Less than 35% CPU idle with other stations live. Keep Kick copy-only."
        )
    if iowait>0.12 or steal>0.10:
        raise RuntimeError(
            "Elevated I/O wait or hypervisor CPU steal; leave live video unchanged."
        )
    has_font=subprocess.run(["docker","exec",CONTAINER,"test","-r",LOCK_FONT],capture_output=True)
    if has_font.returncode:
        raise RuntimeError("Kick container is missing fonts-symbola; install font without restarting.")
    song=read(KICK/"now-playing.json")
    label=" ".join(str(song.get("title") or "Peter Lofi").split())
    label=label[:42]+"..." if len(label)>45 else label
    scratch=KICK/"kick-overlay-preflight.txt"
    scratch_lock=KICK/"kick-overlay-preflight-lock.txt"
    scratch.write_text(label+"\n",encoding="utf-8")
    scratch_lock.write_text("\n",encoding="utf-8")
    try:
        vf=(
            f"drawtext=fontfile={FONT}:text='NOW PLAYING':"
            "fontcolor=white@0.85:fontsize=23:x=56:y=h-105:"
            "shadowcolor=black@0.85:shadowx=2:shadowy=2,"
            f"drawtext=fontfile={FONT}:textfile=/state/kick/{scratch.name}:"
            "fontcolor=white:fontsize=34:x=56:y=h-67:"
            "shadowcolor=black@0.85:shadowx=2:shadowy=2,"
            f"drawtext=fontfile={LOCK_FONT}:textfile=/state/kick/{scratch_lock.name}:"
            "fontcolor=white:fontsize=34:x=56:y=h-67"
        )
        cmd=[
            "docker","exec",CONTAINER,
            "nice","-n","15","ffmpeg","-hide_banner","-loglevel","error",
            "-nostdin","-i",str(source),"-t","8","-map","0:v:0","-an",
            "-filter_threads","1","-vf",vf,
            "-c:v","libx264","-preset","superfast","-threads","2",
            "-bf","0","-pix_fmt","yuv420p","-f","null","-"
        ]
        started=time.monotonic()
        run=subprocess.run(cmd,capture_output=True,text=True,timeout=70)
        elapsed=time.monotonic()-started
        if run.returncode:
            raise RuntimeError("FFmpeg preflight failure: "+run.stderr[-300:])
        speed=8/max(elapsed,0.001)
        print(f"KICK_OVERLAY_SPEED: {speed:.2f}x real-time")
        print(f"AVAILABLE_RAM: {mem_available()/1024**3:.1f} GiB")
        if speed<1.45:
            raise RuntimeError("Kick encoder speed margin is too low while Twitch is live.")
        if mem_available()<3.0*1024**3:
            raise RuntimeError("Memory pressure after benchmark.")
        print("KICK_OVERLAY_PREFLIGHT_PASSED",flush=True)
        print("No live processes or RTMP connections were restarted.",flush=True)
    finally:
        scratch.unlink(missing_ok=True)
        scratch_lock.unlink(missing_ok=True)


if __name__=="__main__":
    try:
        main()
    except (RuntimeError,OSError,subprocess.TimeoutExpired) as err:
        print("KICK_OVERLAY_PREFLIGHT_FAILED:",str(err),file=sys.stderr)
        sys.exit(1)
