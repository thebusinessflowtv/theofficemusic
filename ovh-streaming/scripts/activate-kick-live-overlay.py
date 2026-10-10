#!/usr/bin/env python3
"""Controlled Kick-only dynamic overlay: preserve publisher and audio.

Preflight and a dry-run are REQUIRED before changing the Kick visual child.
Never recreates the Kick, Twitch, YouTube or Twitch-chatbot Docker containers.
WARNING: switching visual senders can cause a brief video interruption.
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone

ROOT=pathlib.Path("/home/ubuntu/theofficemusic/ovh-streaming")
STATE=ROOT/"state"/"kick"
SWITCH=STATE/"visual-overlay-toggle.json"
PUBLISH=STATE/"health.json"
VISUAL=STATE/"visual-health.json"
DESIRED=STATE/"desired.json"
NOW=STATE/"now-playing.json"
BACKUPS=STATE/"overlay-activation-backups"
BACKUP=BACKUPS/"visual_engine.before_dynamic_overlay.py"
CONTAINER="peter-lofi-kick"
TARGET="/app/visual_engine.py"


def load(path):
    try:
        value=json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value,dict) else {}
    except (OSError, ValueError):
        return {}


def atomic(path,payload):
    tmp=path.with_name(path.name+".tmp."+str(os.getpid()))
    try:
        tmp.write_text(json.dumps(payload,indent=2)+"\n",encoding="utf-8")
        os.replace(tmp,path)
    finally:
        tmp.unlink(missing_ok=True)


def docker(*args,timeout=25):
    return subprocess.run(["docker",*args],capture_output=True,text=True,timeout=timeout,check=True)


def check_platform():
    if os.geteuid()!=0:
        raise RuntimeError("Run with sudo on the OVH server.")
    if not (ROOT/"app"/"visual_engine.py").is_file():
        raise RuntimeError("Updated visual engine not found locally. Run git pull --ff-only.")
    if docker("inspect","-f","{{.State.Running}}",CONTAINER).stdout.strip()!="true":
        raise RuntimeError("Kick container is not running; refusing activation.")
    d=load(DESIRED)
    h=load(PUBLISH)
    v=load(VISUAL)
    twitch_health=load(ROOT/"state"/"twitch"/"health.json")
    if twitch_health.get("status")!="live":
        raise RuntimeError("Twitch must be live before overlay switching.")
    if not all((d.get("desired")=="live",h.get("status")=="live",v.get("status")=="streaming")):
        raise RuntimeError("Kick visual and publisher must be live before switching.")
    if not load(NOW).get("track_id"):
        raise RuntimeError("Current Kick music missing; refusing activation.")
    if not h.get("encoder_pid") or not h.get("visual_pid") or not v.get("sender_pid"):
        raise RuntimeError("Publisher/visual PID missing; refusing activation.")
    # Video revision and generation must be left untouched.
    available=0
    for line in pathlib.Path("/proc/meminfo").read_text().splitlines():
        if line.startswith("MemAvailable:"):
            available=int(line.split()[1])*1024
    if available<2.5*1024**3:
        raise RuntimeError("Insufficient RAM headroom for a second video encoder.")
    return d,h,v


def reload_visual():
    # PID must be the Kick visual feeder owned by the StreamCore parent.
    # Only this child gets SIGTERM; the publisher FFmpeg stays in place.
    script=r'''import json,os,signal
h=json.load(open("/state/kick/health.json"))
pid=int(h.get("visual_pid") or 0)
if pid<2:
    raise SystemExit("Invalid visual PID")
cmd=open("/proc/%d/cmdline"%pid,"rb").read().replace(b"\x00",b" ")
if b"/app/visual_engine.py" not in cmd or b"kick" not in cmd:
    raise SystemExit("Visual PID does not match Kick visual child")
os.kill(pid,signal.SIGTERM)
print("Kick visual feeder reload requested; RTMP publisher left running")
'''
    result=docker("exec",CONTAINER,"python","-c",script)
    print(result.stdout.strip(),flush=True)


def monitor(expected,expected_mode,seconds=50):
    initial_pid=int(expected.get("encoder_pid") or 0)
    initial_restarts=int(expected.get("restarts") or 0)
    deadline=time.monotonic()+seconds
    seen=False
    last=""
    while time.monotonic()<deadline:
        time.sleep(2)
        h=load(PUBLISH)
        v=load(VISUAL)
        if (int(h.get("encoder_pid") or 0)!=initial_pid
                or int(h.get("restarts") or 0)!=initial_restarts):
            raise RuntimeError("RTMP encoder changed; rolling back the visual overlay.")
        if h.get("status") not in ("live","starting"):
            raise RuntimeError("Kick publisher unhealthy: "+str(h.get("status")))
        if v.get("status")=="error":
            raise RuntimeError("Visual sender failure: "+str(v.get("error") or "")[:180])
        if (h.get("status")=="live"
                and v.get("status")=="streaming"
                and v.get("overlay_mode")==expected_mode
                and int(v.get("sender_pid") or 0)>0
                and int(v.get("sender_pid"))!=int(expected.get("sender_pid") or 0)):
            seen=True
            last="PASS"
        elif seen:
            last="WAIT"
        if not seen and time.monotonic()>deadline-10:
            print("Waiting for visual sender to resume...",flush=True)
    if not seen:
        raise RuntimeError("New Kick visual sender was not confirmed as "+expected_mode)
    return last


def rollback(original,reason):
    print("VISUAL_ROLLBACK_BEGIN",reason,flush=True)
    atomic(SWITCH,{"enabled":False,"updated_at":datetime.now(timezone.utc).isoformat(),"reason":reason})
    if BACKUP.is_file():
        docker("cp",str(BACKUP),CONTAINER+":"+TARGET,timeout=30)
    # Parent RTMP process and unrelated streams remain untouched.
    reload_visual()
    print("VISUAL_ROLLBACK_REQUESTED; check Kick preview and health status.",flush=True)


def activate():
    desired,base,visual=check_platform()
    if visual.get("overlay_mode")=="dynamic_now_playing":
        print("KICK_OVERLAY_ALREADY_ACTIVE");return
    if os.environ.get("KICK_NOW_PLAYING_OVERLAY")=="1":
        raise RuntimeError("Kick external env already forces overlay; stop.")
    test=subprocess.run([sys.executable,str(ROOT/"scripts"/"preflight-kick-now-playing.py")],
                        capture_output=True,text=True,timeout=100)
    print(test.stdout.strip(),flush=True)
    if test.returncode or "OVERLAY_PREFLIGHT_PASSED" not in test.stdout:
        raise RuntimeError("Preflight failed. "+test.stderr[-250:])
    # Prevent concurrent overlay deployment.
    print("Preparing backups and validating Kick visual child only...",flush=True)
    BACKUPS.mkdir(parents=True,exist_ok=True)
    if not BACKUP.is_file():
        docker("cp",CONTAINER+":"+TARGET,str(BACKUP),timeout=30)
        os.chmod(BACKUP,0o600)
    updated=ROOT/"app"/"visual_engine.py"
    subprocess.run([sys.executable,"-m","py_compile",str(updated)],check=True,timeout=20)
    current=load(SWITCH)
    if current.get("enabled") is True:
        raise RuntimeError("Overlay toggle was already enabled. No changes made.")
    initial={"encoder_pid":base["encoder_pid"],"restarts":base.get("restarts",0),
             "sender_pid":visual.get("sender_pid")}
    # Do not change generation, visual_revision, stream metadata or music queue.
    docker("cp",str(updated),CONTAINER+":"+TARGET,timeout=30)
    reload_started=False
    try:
        docker("exec",CONTAINER,"python","-m","py_compile",TARGET)
        atomic(SWITCH,{"enabled":True,"updated_at":datetime.now(timezone.utc).isoformat(),
                       "source":"kick-dynamic-overlay-hot-visual-reload"})
        reload_started=True
        reload_visual()
        monitor(initial,"dynamic_now_playing",seconds=45)
        print("KICK_OVERLAY_ACTIVE: live song title enabled.",flush=True)
        print("Commands appear only after the separate Kick chat bot is subscribed.",flush=True)
        print("RTMP ENCODER UNCHANGED. Check picture, audio and track-title updates in Kick preview.",flush=True)
    except Exception as exc:
        try:
            if reload_started:
                rollback(initial,str(exc)[:200])
            else:
                # Source verification failed before reload; preserve the old
                # visual process and publisher with no interruption at all.
                docker("cp",str(BACKUP),CONTAINER+":"+TARGET,timeout=30)
                atomic(SWITCH,{"enabled":False,"reason":"pre-reload-check-failed"})
                print("No visual restart occurred; original script restored.",flush=True)
        except Exception as rescue:
            print("ROLLBACK_ATTEMPT_FAILED:",str(rescue)[:250],file=sys.stderr)
        raise



# The refresh path is distinct from --activate: the latter correctly refuses
# to make changes once the dynamic overlay is already active.
REFRESH_BACKUP=BACKUPS/"visual_engine.before_last_refresh.py"


def refresh(undo=False):
    desired,base,visual=check_platform()
    if visual.get("overlay_mode")!="dynamic_now_playing" or not load(SWITCH).get("enabled"):
        raise RuntimeError("Dynamic Kick overlay must already be active.")
    if undo and not REFRESH_BACKUP.is_file():
        raise RuntimeError("No previous overlay-layout backup exists.")
    updated=REFRESH_BACKUP if undo else ROOT/"app"/"visual_engine.py"
    if not updated.is_file():
        raise RuntimeError("Updated overlay script not available.")
    if not undo:
        font="/usr/share/fonts/truetype/ancient-scripts/Symbola_hint.ttf"
        check=docker("exec",CONTAINER,"python","-c",
                     "from pathlib import Path; print(Path("+repr(font)+").is_file())")
        if check.stdout.strip()!="True":
            raise RuntimeError(
                "Lock emoji outline font missing from Kick container. "
                "Install fonts-symbola INSIDE that container first; "
                "do not recreate the live container."
            )
    subprocess.run([sys.executable,"-m","py_compile",str(updated)],check=True,timeout=20)
    BACKUPS.mkdir(parents=True,exist_ok=True)
    if not undo:
        # Always capture the ACTIVE source before replacing it. This lets a
        # failed refresh restore the working overlay, not the pre-overlay copy.
        docker("cp",CONTAINER+":"+TARGET,str(REFRESH_BACKUP),timeout=30)
        os.chmod(REFRESH_BACKUP,0o600)
    old_snapshot=BACKUPS/"visual_engine.before_this_operation.py"
    docker("cp",CONTAINER+":"+TARGET,str(old_snapshot),timeout=30)
    initial={"encoder_pid":base["encoder_pid"],"restarts":base.get("restarts",0),
             "sender_pid":visual.get("sender_pid")}
    reload_started=False
    try:
        docker("cp",str(updated),CONTAINER+":"+TARGET,timeout=30)
        docker("exec",CONTAINER,"python","-m","py_compile",TARGET)
        reload_started=True
        reload_visual()
        monitor(initial,"dynamic_now_playing",seconds=45)
        print("OVERLAY_"+("PREVIOUS_STYLE_RESTORED" if undo else "STYLE_UPDATED"),flush=True)
        print("Kick RTMP, audio, Twitch, and YouTube were not intentionally restarted.",flush=True)
    except Exception as exc:
        try:
            docker("cp",str(old_snapshot),CONTAINER+":"+TARGET,timeout=30)
            if reload_started:
                reload_visual()
            print("VISUAL_STYLE_ROLLBACK_REQUESTED; check Kick live preview.",flush=True)
        except Exception as rescue:
            print("VISUAL_STYLE_ROLLBACK_FAILED",str(rescue)[:230],file=sys.stderr)
        raise
    finally:
        old_snapshot.unlink(missing_ok=True)


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("--activate",action="store_true",help="Hot-reload only Kick visual child")
    parser.add_argument("--status",action="store_true",help="Read-only Kick overlay state")
    parser.add_argument("--rollback",action="store_true",help="Restore original visual child, without restarting publisher")
    parser.add_argument("--refresh",action="store_true",help="Refresh the active dynamic overlay style; visual child only")
    parser.add_argument("--undo-refresh",action="store_true",help="Restore the last overlay style if the new layout is unwanted")
    args=parser.parse_args()
    if args.status:
        print("Publisher:",load(PUBLISH).get("status"))
        print("Visual:",load(VISUAL).get("status"))
        print("Overlay:",load(VISUAL).get("overlay_mode","unreported"))
        print("Toggle enabled:",load(SWITCH).get("enabled",False))
        return
    if args.rollback:
        if os.geteuid()!=0:
            raise RuntimeError("Run with sudo on the OVH server.")
        if not BACKUP.is_file():
            raise RuntimeError("No visual engine backup exists, so rollback is unsafe.")
        if not load(SWITCH).get("enabled"):
            print("OVERLAY_TOGGLE_ALREADY_DISABLED. Check video preview.")
            return
        rollback(load(PUBLISH),"manual")
        return
    if args.refresh or args.undo_refresh:
        refresh(undo=args.undo_refresh)
        return
    if not args.activate:
        parser.error("Select --status, --activate, --refresh, --undo-refresh or --rollback.")
    activate()


if __name__=="__main__":
    try:
        main()
    except (OSError,ValueError,RuntimeError,subprocess.CalledProcessError,
            subprocess.TimeoutExpired) as exc:
        print("KICK_OVERLAY_NOT_CONFIRMED",type(exc).__name__,str(exc)[:400],file=sys.stderr)
        sys.exit(1)
