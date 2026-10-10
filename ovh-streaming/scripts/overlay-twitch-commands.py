#!/usr/bin/env python3
"""Prepare/apply one-time static chat-command caption on the CURRENT Twitch video.

Renders in a separate low-priority FFmpeg job (not the RTMP encoder) and
atomically replaces only the Twitch visual cache after safety checks.
Does NOT rebuild/restart Twitch/Kick/YouTube encoders or audio containers.
Usage:
  sudo python3 scripts/overlay-twitch-commands.py --prepare
  sudo python3 scripts/overlay-twitch-commands.py --activate
"""
from __future__ import annotations
import argparse
import fcntl
import hashlib
import json
import os
import pathlib
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone

ROOT=pathlib.Path("/home/ubuntu/theofficemusic/ovh-streaming")
STATE=ROOT/"state"
TWITCH=STATE/"twitch"
CACHE=(TWITCH/"visual-cache").resolve()
STAGING=TWITCH/"visual-commands-stage"
MANIFEST=STAGING/"manifest.json"
BACKUPS=TWITCH/"visual-commands-backups"
SOURCE_PREFIX=pathlib.Path("/state/twitch/visual-cache")
CONTAINER="peter-lofi-twitch"
COMMAND_TEXT="!skip     !song     !back     !freeze"
FONT="/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"


def load(path):
    try:return json.loads(path.read_text(encoding="utf-8"))
    except (OSError,ValueError):return {}


def save(path,value):
    path.parent.mkdir(parents=True,exist_ok=True)
    tmp=path.with_name(path.name+".tmp-"+str(os.getpid()))
    try:
        with tmp.open("x",encoding="utf-8") as f:
            json.dump(value,f,indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp,path)
    finally:tmp.unlink(missing_ok=True)


def docker(*argv,capture=False,timeout=240):
    return subprocess.run(["docker","exec",CONTAINER,*argv],check=True,
                          capture_output=capture,text=capture,timeout=timeout)


def ffprobe(video):
    out=docker("ffprobe","-v","error","-select_streams","v:0",
               "-show_entries","stream=codec_name,width,height,r_frame_rate:format=duration",
               "-of","json",str(video),capture=True,timeout=30)
    data=json.loads(out.stdout)
    return (data.get("streams") or [{}])[0],float(data["format"].get("duration") or 0)


def checksum(path):
    hash=hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda:f.read(1024*1024),b""):hash.update(chunk)
    return hash.hexdigest()


def active_state():
    desired=load(TWITCH/"desired.json")
    health=load(TWITCH/"visual-health.json")
    publisher=load(TWITCH/"health.json")
    if desired.get("desired")!="live" or publisher.get("status")!="live":
        raise RuntimeError("Twitch is not confirmed live; refusing video change.")
    if health.get("status")!="streaming":
        raise RuntimeError("Current visual sender not healthy; refusing video change.")
    loop_url=str(desired.get("loop_url") or "")
    if not loop_url or str(health.get("loop_url") or "")!=loop_url:
        raise RuntimeError("Visual sender loop URL differs from current desired video.")
    current=pathlib.Path(str(health.get("prepared_file") or ""))
    if not current.is_absolute() or not current.is_relative_to(SOURCE_PREFIX):
        raise RuntimeError("Current video is not a Twitch cached video.")
    host=(CACHE/current.relative_to(SOURCE_PREFIX)).resolve()
    if not host.is_relative_to(CACHE) or not host.is_file():
        raise RuntimeError("Twitch cached visual file missing.")
    return desired,health,current,host


def cmdfilter():
    # Static labels are baked into the loop ONLY ONCE; streaming still
    # uses FFmpeg -c:v copy, with no perpetual CPU-consuming drawtext filter.
    # Source is normalized 1920x1080 by VisualEngine.prepare.
    return (
        "drawbox=x=iw-800:y=ih-118:w=766:h=83:color=black@0.58:t=fill,"
        f"drawtext=fontfile={FONT}:"
        f"text='{COMMAND_TEXT}':fontcolor=white:fontsize=30:"
        "x=w-text_w-57:y=h-92"
    )


def prepare():
    desired,health,containerpath,host=active_state()
    STAGING.mkdir(parents=True,exist_ok=True)
    metadata=load(MANIFEST)
    if (metadata.get("current_host")==str(host)
        and metadata.get("loop_url")==desired["loop_url"]
        and (STAGING/"commands-ready.mp4").is_file()
        and checksum(host)==metadata.get("original_sha256")):
        print("Caption video already prepared for this source. Use --activate.")
        return
    if metadata.get("installed_sha256")==checksum(host):
        print("Caption already installed on current Twitch video.")
        return
    source,seconds=ffprobe(containerpath)
    if source.get("codec_name")!="h264" or int(source.get("width") or 0)!=1920 or int(source.get("height") or 0)!=1080:
        raise RuntimeError("Expected pre-encoded 1920x1080 H264 Twitch clip; not modifying.")
    if not 1<=seconds<=120:
        raise RuntimeError("Clip is longer than 120 sec or invalid; aborting to protect live CPU.")
    docker("sh","-c",f"test -r '{FONT}' && ffmpeg -hide_banner -filters | grep -q drawtext",timeout=15)
    free=shutil.disk_usage(STATE).free
    if free < host.stat().st_size*4+300*1024*1024:
        raise RuntimeError("Insufficient free disk for safe overlay/backup.")
    temp=STAGING/"commands-ready.mp4"
    temp.unlink(missing_ok=True)
    bitrate=int(health.get("video_bitrate_kbps") or 4500)
    bitrate=max(2000,min(6500,bitrate))
    container_temp="/state/twitch/visual-commands-stage/commands-ready.mp4"
    print(f"Preparing caption on {seconds:.1f}s video with low-priority offline encoder...",flush=True)
    try:
        docker(
            "nice","-n","15","ffmpeg","-hide_banner","-loglevel","warning","-nostdin","-y",
            "-i",str(containerpath),"-map","0:v:0","-an",
            "-vf",cmdfilter(),"-threads","2","-filter_threads","1",
            "-c:v","libx264","-preset","superfast","-tune","zerolatency",
            "-profile:v","main","-bf","0","-pix_fmt","yuv420p",
            "-b:v",str(bitrate)+"k","-minrate",str(bitrate)+"k","-maxrate",str(bitrate)+"k",
            "-bufsize",str(bitrate*2)+"k","-g","60","-keyint_min","60",
            "-sc_threshold","0","-movflags","+faststart",container_temp,
            timeout=900
        )
    except Exception:
        temp.unlink(missing_ok=True)
        raise
    info,duration=ffprobe(pathlib.Path(container_temp))
    if not temp.is_file() or temp.stat().st_size<1024 or info.get("codec_name")!="h264" or abs(duration-seconds)>2:
        temp.unlink(missing_ok=True)
        raise RuntimeError("Rendered overlay video failed validation; original live video unchanged.")
    save(MANIFEST,{
        "current_host":str(host),"loop_url":desired["loop_url"],
        "original_sha256":checksum(host),"rendered_sha256":checksum(temp),
        "ready_at":datetime.now(timezone.utc).isoformat(),
        "seconds":seconds,"size_bytes":temp.stat().st_size,
        "installed_sha256":None
    })
    print("OVERLAY_PREPARED: command labels ready. Live publisher unchanged.")
    print("Run --activate to apply to the current Twitch video.")


def activate():
    desired,health,containerpath,host=active_state()
    meta=load(MANIFEST)
    ready=STAGING/"commands-ready.mp4"
    if meta.get("installed_sha256") and checksum(host)==meta["installed_sha256"]:
        print("OVERLAY_ALREADY_ACTIVE");return
    if not ready.is_file() or meta.get("current_host")!=str(host) or meta.get("loop_url")!=desired["loop_url"]:
        raise RuntimeError("Prepared video mismatch. Run --prepare first.")
    if checksum(host)!=meta.get("original_sha256") or checksum(ready)!=meta.get("rendered_sha256"):
        raise RuntimeError("Video changed since preparation. Refusing stale swap.")
    # Stream copies a file opened by its own visual sender. Atomic replacement
    # is safe while it reads the original inode. Revision changes ONLY sender.
    BACKUPS.mkdir(parents=True,exist_ok=True)
    backup=BACKUPS/(host.stem+"-before-commands.mp4")
    if not backup.is_file():
        shutil.copy2(host,backup)
    next_revision=str(int(desired.get("visual_revision") or 0)+1)
    os.replace(ready,host)
    fresh=load(TWITCH/"desired.json")
    if (fresh.get("loop_url")!=desired.get("loop_url")
        or fresh.get("desired")!=desired.get("desired")
        or fresh.get("visual_revision")!=desired.get("visual_revision")):
        shutil.copy2(backup,host)
        raise RuntimeError("Live desired state changed. Overlay aborted and original restored.")
    fresh["visual_revision"]=int(next_revision)
    save(TWITCH/"desired.json",fresh)
    print("Asking Twitch video sender to reload the cached captioned clip...",flush=True)
    for _ in range(30):
        time.sleep(1)
        status=load(TWITCH/"visual-health.json")
        if (status.get("status")=="streaming"
            and str(status.get("visual_revision"))==next_revision
            and status.get("sender_pid")):
            meta["installed_sha256"]=checksum(host)
            save(MANIFEST,meta)
            print("OVERLAY_ACTIVE: !skip !song !back !freeze at lower right.")
            print("RTMP encoder/container and audio were not restarted.")
            return
        if status.get("status")=="error":
            break
    # On failure undo the overlay and ask sender to re-open original clip.
    shutil.copy2(backup,host)
    rollback=load(TWITCH/"desired.json")
    if rollback.get("loop_url")==fresh.get("loop_url"):
        rollback["visual_revision"]=int(next_revision)+1
        save(TWITCH/"desired.json",rollback)
    raise RuntimeError("Overlay sender not confirmed; original clip restored for recovery.")


def main():
    arg=argparse.ArgumentParser()
    action=arg.add_mutually_exclusive_group(required=True)
    action.add_argument("--prepare",action="store_true")
    action.add_argument("--activate",action="store_true")
    args=arg.parse_args()
    if os.geteuid()!=0:raise RuntimeError("Run using sudo on the OVH server.")
    if not ROOT.is_dir():raise RuntimeError("OVH streaming directory not found.")
    lock_path=TWITCH/"visual-commands.lock"
    with lock_path.open("a+b") as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        if args.prepare:prepare()
        else:activate()


if __name__=="__main__":
    try:main()
    except (RuntimeError,subprocess.CalledProcessError,subprocess.TimeoutExpired,
            BlockingIOError,OSError,ValueError) as error:
        print("COMMAND_OVERLAY_FAILED",str(error)[:450],file=sys.stderr)
        sys.exit(1)
