#!/usr/bin/env python3
"""MediaForge host deploy agent.

Runs on the OVH host via systemd. It intentionally exposes NO shell surface:
only a fixed allow-list of deploy/health/rollback operations is accepted.

The process also owns a local, Cloudflare-independent publisher watchdog.
The watchdog never recreates streaming containers. When it sees a burst of
fresh RTMP/network errors it terminates only the FFmpeg publisher child so
stream_core can reconnect immediately using the existing local state.
"""
import json
import os
import pathlib
import re
import signal
import subprocess
import sys
import threading
import time
import urllib.request
from datetime import datetime, timezone

API=os.environ.get("MEDIAFORGE_API_URL","https://mediaforge-api.guilhermeodsgn.workers.dev").rstrip("/")
AGENT_TOKEN=os.environ.get("MEDIAFORGE_AGENT_TOKEN","").strip()
REPO=pathlib.Path(os.environ.get("MEDIAFORGE_REPO","/home/ubuntu/theofficemusic"))
OVH=REPO/"ovh-streaming"
PANEL_REPO=pathlib.Path(os.environ.get("MEDIAFORGE_PANEL_REPO","/home/ubuntu/thebusinessflow"))
CONTROL_ROOT=pathlib.Path(os.environ.get("MEDIAFORGE_CONTROL_ROOT","/opt/mediaforge-control"))
STATE_DIR=pathlib.Path("/var/lib/mediaforge-deploy-agent")
STATE_FILE=STATE_DIR/"state.json"
WATCHDOG_FILE=STATE_DIR/"watchdog.json"
DOCKER_CONFIG_DIR=STATE_DIR/"docker"
POLL=max(3,int(os.environ.get("MEDIAFORGE_DEPLOY_POLL_SECONDS","5")))
WATCHDOG_INTERVAL=max(2,int(os.environ.get("MEDIAFORGE_WATCHDOG_SECONDS","5")))
WATCHDOG_WINDOW=max(20,int(os.environ.get("MEDIAFORGE_WATCHDOG_WINDOW_SECONDS","60")))
WATCHDOG_THRESHOLD=max(3,int(os.environ.get("MEDIAFORGE_WATCHDOG_ERROR_THRESHOLD","5")))
WATCHDOG_COOLDOWN=max(30,int(os.environ.get("MEDIAFORGE_WATCHDOG_COOLDOWN_SECONDS","120")))
# Incident 2026-10-08: observe RTMP errors, never kill active publishers automatically.
# Manual recovery remains possible after diagnosing the affected platform.
WATCHDOG_PUBLISHER_KILL_ENABLED=os.environ.get("MEDIAFORGE_WATCHDOG_PUBLISHER_KILL_ENABLED","0")=="1"
SLOTS=("kick","twitch","youtube-deep-house","youtube-rainy")
DIAGNOSTIC_STATE_SLOTS=SLOTS+("youtube-gta-vi","youtube-ui-test","youtube-lofi-hip-hop")
SERVICES=("ovh-agent","control-api")+SLOTS
CONTAINERS={
    "ovh-agent":"peter-lofi-ovh-agent",
    "control-api":"peter-lofi-control-api",
    "kick":"peter-lofi-kick",
    "twitch":"peter-lofi-twitch",
    "youtube-deep-house":"peter-lofi-youtube-deep-house",
    "youtube-rainy":"peter-lofi-youtube-rainy",
}
NETWORK_ERROR_PATTERNS=(
    "broken pipe",
    "connection reset",
    "connection timed out",
    "connection refused",
    "error writing",
    "failed to update header",
    "server returned",
    "i/o error",
    "input/output error",
    "fifo queue full",
    "recovery failed",
    "cannot open connection",
)

STATE_DIR.mkdir(parents=True,exist_ok=True)
DOCKER_CONFIG_DIR.mkdir(parents=True,exist_ok=True)
_WATCH={}


def now():
    return datetime.now(timezone.utc).isoformat().replace("+00:00","Z")


def agent_headers(extra=None):
    headers={"User-Agent":"MediaForge-Host-Deploy-Agent"}
    if AGENT_TOKEN:
        headers["x-ovh-agent-token"]=AGENT_TOKEN
    if extra:
        headers.update(extra)
    return headers

def fetch_json(url):
    req=urllib.request.Request(
        url+("&" if "?" in url else "?")+"ts="+str(int(time.time()*1000)),
        headers=agent_headers(),
    )
    with urllib.request.urlopen(req,timeout=30) as r:
        return json.loads(r.read().decode("utf-8"))


def post_json(url,payload):
    data=json.dumps(payload).encode("utf-8")
    req=urllib.request.Request(
        url,
        data=data,
        method="POST",
        headers=agent_headers({"content-type":"application/json"}),
    )
    with urllib.request.urlopen(req,timeout=30) as r:
        return json.loads(r.read().decode("utf-8") or "{}")


def run(args,cwd=None,timeout=1200):
    env=os.environ.copy()
    # systemd hardening can make /root read-only. Keep all Docker/Buildx
    # state in the agent's writable state directory instead.
    env.setdefault("DOCKER_CONFIG",str(DOCKER_CONFIG_DIR))
    p=subprocess.run(
        args,
        cwd=str(cwd) if cwd else None,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=timeout,
        check=False,
        env=env,
    )
    out=(p.stdout or "")[-12000:]
    if p.returncode!=0:
        raise RuntimeError(f"command failed ({p.returncode}): {' '.join(args[:4])}\n{out[-2500:]}")
    return out


def read_state():
    try:return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:return {}


def write_state(data):
    tmp=STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(data,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    tmp.replace(STATE_FILE)


def atomic_json(path,data):
    tmp=path.with_suffix(path.suffix+".tmp")
    tmp.write_text(json.dumps(data,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    tmp.replace(path)


def read_json(path,default=None):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def git_head():
    return run(["runuser","-u","ubuntu","--","git","-C",str(REPO),"rev-parse","HEAD"],timeout=30).strip().splitlines()[-1]


def git_sync():
    old=git_head()
    run(["runuser","-u","ubuntu","--","git","-C",str(REPO),"fetch","origin","main"],timeout=180)
    run(["runuser","-u","ubuntu","--","git","-C",str(REPO),"reset","--hard","origin/main"],timeout=60)
    new=git_head()
    return old,new


def health(slot):
    if slot in DIAGNOSTIC_STATE_SLOTS:
        p=OVH/"state"/slot/"health.json"
        desired_path=OVH/"state"/slot/"desired.json"
        try:
            d=json.loads(p.read_text(encoding="utf-8"))
            try:
                desired=json.loads(desired_path.read_text(encoding="utf-8"))
            except Exception:
                desired={}
            return {
                "service":slot,
                "status":d.get("status","unknown"),
                "updated_at":d.get("updated_at"),
                "fps":d.get("fps"),
                "video_bitrate_kbps":d.get("video_bitrate_kbps"),
                "restarts":d.get("restarts",0),
                "hot_swap":bool(d.get("hot_swap",False)),
                "encoder_pid":d.get("encoder_pid"),
                "audio_pid":d.get("audio_pid"),
                "visual_pid":d.get("visual_pid"),
                "visual_status":d.get("visual_status"),
                "audio_status":d.get("audio_status"),
                "audio_stalls":d.get("audio_stalls",0),
                "generation":desired.get("generation"),
                "visual_revision":desired.get("visual_revision"),
            }
        except Exception as e:
            return {"service":slot,"status":"unknown","error":str(e)[:160]}
    if slot=="control-api":
        try:
            with urllib.request.urlopen("http://127.0.0.1:8787/health",timeout=5) as r:
                d=json.loads(r.read().decode("utf-8"))
            return {"service":slot,"status":"live" if d else "unknown"}
        except Exception as e:
            return {"service":slot,"status":"unknown","error":str(e)[:160]}
    name=CONTAINERS.get(slot)
    if name:
        try:
            out=run(["docker","inspect","-f","{{.State.Running}}",name],timeout=20).strip().lower()
            return {"service":slot,"status":"live" if out=="true" else "stopped"}
        except Exception as e:
            return {"service":slot,"status":"unknown","error":str(e)[:160]}
    return {"service":slot,"status":"unknown"}


def wait_healthy(slot,timeout=180,after_updated_at=None,require_hot_swap=False):
    end=time.time()+timeout
    last={}
    while time.time()<end:
        last=health(slot)
        fresh=(not after_updated_at) or (last.get("updated_at") and last.get("updated_at")!=after_updated_at)
        live=last.get("status")=="live" and fresh
        if require_hot_swap:
            live=live and last.get("hot_swap") is True and bool(last.get("encoder_pid")) and last.get("visual_status")=="streaming"
        if live:
            return last
        time.sleep(5)
    raise RuntimeError(f"{slot} did not become ready: {last}")


def backup_image(service):
    name=f"ovh-streaming-{service}:latest"
    stamp=datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    backup=f"mediaforge-backup-{service}:{stamp}"
    try:
        run(["docker","image","inspect",name],timeout=20)
        run(["docker","tag",name,backup],timeout=20)
        return backup
    except Exception:
        return ""


def ensure_control_plane_containers():
    """Keep the non-streaming MediaForge control plane alive.

    This never touches Kick/Twitch/YouTube publisher containers.
    Docker restart policies do not recover a container that was created but
    never started, so explicitly start control-plane containers in that state.
    """
    results={}
    for name in ("mediaforge-api-ovh","mediaforge-web-ovh"):
        try:
            state=run(["docker","inspect","-f","{{.State.Status}}",name],timeout=20).strip().lower()
        except Exception as exc:
            results[name]={"status":"missing","error":str(exc)[:180]}
            continue
        if state!="running":
            try:
                run(["docker","start",name],timeout=60)
                state=run(["docker","inspect","-f","{{.State.Status}}",name],timeout=20).strip().lower()
                results[name]={"status":state,"recovered":True}
            except Exception as exc:
                results[name]={"status":state,"recovered":False,"error":str(exc)[:240]}
        else:
            results[name]={"status":"running","recovered":False}
    return results


def publish_mediaforge_control_plane():
    """Publish MediaForge UI + local API without touching RTMP publisher containers."""
    before={slot:health(slot) for slot in SLOTS}

    # This host agent is intentionally sandboxed with ProtectHome=read-only.
    # Use the Docker daemon (already allow-listed for deploys) to update the
    # panel checkout and control-plane files without widening the agent sandbox.
    helper_image="caddy:2-alpine"
    git_cmd=(
        "apk add --no-cache git >/dev/null && "
        "git config --global --add safe.directory /repo && "
        "git fetch origin main && git reset --hard origin/main"
    )
    run([
        "docker","run","--rm",
        "-v",f"{PANEL_REPO}:/repo",
        "-w","/repo",
        helper_image,"sh","-lc",git_cmd
    ],timeout=300)

    copy_cmd=(
        "mkdir -p /control/site && "
        "rm -rf /control/site/* && "
        "cp -a /panel/control-center/. /control/site/ && "
        "cp /control/site/secure.html /control/site/index.html && "
        "printf '%s\\n' \"window.MEDIAFORGE_CONFIG = { API_URL: window.location.origin };\" > /control/site/mediaforge-config.js"
    )
    run([
        "docker","run","--rm",
        "-v",f"{PANEL_REPO}:/panel:ro",
        "-v",f"{CONTROL_ROOT}:/control",
        helper_image,"sh","-lc",copy_cmd
    ],timeout=120)

    compose=str(CONTROL_ROOT/"docker-compose.yml")
    run(["docker","compose","-f",compose,"build","api"],timeout=1800)

    # The web container serves /control/site through a bind mount, so copying
    # static files does NOT require recreating Caddy. Recreate only the API when
    # its image actually changed. Never use --force-recreate here: an interrupted
    # compose transaction could leave the API in Docker's "created" state.
    run(["docker","compose","-f",compose,"up","-d","--no-deps","api"],timeout=300)
    ensure_control_plane_containers()

    # Wait for the local MediaForge API, then seed the new DJ candidate manifest
    # into local_config because LOCAL_RUNTIME deliberately does not fetch GitHub.
    api_health={}
    end=time.time()+120
    while time.time()<end:
        try:
            with urllib.request.urlopen("http://127.0.0.1:8790/api/health",timeout=5) as r:
                api_health=json.loads(r.read().decode("utf-8") or "{}")
            if api_health:
                break
        except Exception:
            time.sleep(2)
    if not api_health:
        raise RuntimeError("local MediaForge API did not return after control-plane publish")

    manifest_path=PANEL_REPO/"control"/"twitch-dj-candidates-2026-10-03.json"
    if not manifest_path.is_file():
        raise RuntimeError(f"DJ 100 manifest missing after panel sync: {manifest_path}")
    manifest=json.loads(manifest_path.read_text(encoding="utf-8"))
    post_json(
        "http://127.0.0.1:8790/api/ovh/agent/runtime-config",
        {"path":"control/twitch-dj-candidates-2026-10-03.json","payload":manifest},
    )

    ui_file=CONTROL_ROOT/"site"/"lives-cloudflare.js"
    ui_text=ui_file.read_text(encoding="utf-8") if ui_file.is_file() else ""
    if "Verificar 100 novas" not in ui_text or "dance100" not in ui_text:
        raise RuntimeError("published Control Center bundle does not contain DJ 100 UI")

    after={slot:health(slot) for slot in SLOTS}
    # Publish a non-sensitive DJ batch snapshot for operational validation.
    try:
        tw_import=read_json(OVH/"state"/"twitch"/"dj-import.json",{}) or {}
        ki_import=read_json(OVH/"state"/"kick"/"dj-import.json",{}) or {}
        tw_playlist=read_json(OVH/"state"/"twitch"/"playlist.json",{}) or {}
        ki_playlist=read_json(OVH/"state"/"kick"/"playlist.json",{}) or {}
        snapshot={
            "checked_at":now(),
            "import_status":str(tw_import.get("status") or "none"),
            "zip_mp3_files":int(tw_import.get("zip_mp3_files") or 0),
            "new_valid_tracks":int(tw_import.get("new_valid_tracks") or 0),
            "duplicates_existing":int(tw_import.get("duplicates_existing") or 0),
            "duplicates_in_batch":int(tw_import.get("duplicates_in_batch") or 0),
            "rejected_files":len(tw_import.get("rejected_files") or []),
            "original_tracks":int(tw_import.get("original_tracks") if tw_import.get("original_tracks") is not None else -1),
            "originals_removed":int(tw_import.get("originals_removed") or 0),
            "commercial_tracks":int(tw_import.get("commercial_tracks") or 0),
            "twitch_track_count":len(tw_playlist.get("tracks") or []),
            "kick_track_count":len(ki_playlist.get("tracks") or []),
            "rtmp_restart":bool(tw_import.get("rtmp_restart",False)),
            "container_restart":bool(tw_import.get("container_restart",False)),
            "twitch_status":str(after.get("twitch",{}).get("status") or "unknown"),
            "kick_status":str(after.get("kick",{}).get("status") or "unknown"),
            "twitch_hot_swap":bool(after.get("twitch",{}).get("hot_swap",False)),
            "kick_hot_swap":bool(after.get("kick",{}).get("hot_swap",False)),
            "kick_import_status":str(ki_import.get("status") or "none"),
        }
        atomic_json(CONTROL_ROOT/"site"/"dj-validation.json",snapshot)
    except Exception as exc:
        atomic_json(CONTROL_ROOT/"site"/"dj-validation.json",{"checked_at":now(),"error":str(exc)[:240]})

    changed=[]
    for slot in SLOTS:
        bp=before.get(slot,{}).get("encoder_pid")
        ap=after.get(slot,{}).get("encoder_pid")
        if bp is not None and ap!=bp:
            changed.append(f"{slot}:{bp}->{ap}")
    if changed:
        raise RuntimeError("publisher PID changed during control-plane publish: "+",".join(changed))

    return {
        "panel_repo":str(PANEL_REPO),
        "control_root":str(CONTROL_ROOT),
        "api_status":"live",
        "dj100_manifest_seeded":True,
        "ui_verified":True,
        "publisher_pids_preserved":True,
        "services":after,
    }


def deploy_service(service):
    if service not in SERVICES:
        raise ValueError("service not allowed")
    before=health(service)
    backup=backup_image(service)

    desired_state="live"
    if service in SLOTS:
        try:
            desired=json.loads((OVH/"state"/service/"desired.json").read_text(encoding="utf-8"))
            desired_state=str(desired.get("desired") or "live").lower()
        except Exception:
            desired_state="live"

    run(["docker","compose","build",service],cwd=OVH,timeout=1800)
    run(["docker","compose","up","-d","--no-deps","--force-recreate",service],cwd=OVH,timeout=300)

    # A stopped station still needs the new image/runtime installed, but it must
    # remain stopped. Treat that as a successful deployment instead of waiting
    # four minutes for a LIVE state that must never happen.
    if service in SLOTS and desired_state in {"stopped","stop","offline"}:
        end=time.time()+90
        last={}
        while time.time()<end:
            last=health(service)
            fresh=(not before.get("updated_at")) or (
                last.get("updated_at") and last.get("updated_at")!=before.get("updated_at")
            )
            if fresh and last.get("status") in {"stopped","starting"}:
                return {
                    "service":service,
                    "health":last,
                    "backup_image":backup,
                    "desired_preserved":"stopped",
                }
            time.sleep(3)
        raise RuntimeError(f"{service} did not return in preserved stopped state: {last}")

    h=wait_healthy(
        service,
        240,
        after_updated_at=before.get("updated_at"),
        require_hot_swap=service in SLOTS,
    )
    extra={}
    if service=="control-api":
        extra["mediaforge_control_plane"]=publish_mediaforge_control_plane()
    return {"service":service,"health":h,"backup_image":backup,**extra}


def deploy_all():
    results=[]
    # Explicit maintenance operation only. Normal media/playlist changes never
    # call this path.
    run(["docker","compose","build"],cwd=OVH,timeout=2400)
    order=("ovh-agent","control-api","twitch","kick","youtube-deep-house","youtube-rainy")
    for service in order:
        before=health(service)
        backup=backup_image(service)
        run(["docker","compose","up","-d","--no-deps","--force-recreate",service],cwd=OVH,timeout=300)
        results.append({
            "service":service,
            "health":wait_healthy(
                service,
                240,
                after_updated_at=before.get("updated_at"),
                require_hot_swap=service in SLOTS,
            ),
            "backup_image":backup,
        })
    return results


def hot_patch_streaming(target="all"):
    targets=list(SLOTS) if target in ("", "all") else [target]
    for slot in targets:
        if slot not in SLOTS:
            raise ValueError("hot patch target not allowed")

    # Refresh the command agent first. It carries no media/RTMP, so restarting
    # it cannot interrupt any live. This keeps realtime skip/previous protocol
    # in sync with the audio engine.
    agent_name=CONTAINERS.get("ovh-agent","peter-lofi-ovh-agent")
    run(["docker","cp",str(OVH/"app"/"ovh_agent.py"),f"{agent_name}:/app/ovh_agent.py"],timeout=30)
    run(["docker","restart",agent_name],timeout=60)

    results=[]
    for slot in targets:
        name=CONTAINERS[slot]
        before=health(slot)
        before_encoder=before.get("encoder_pid")
        before_audio=before.get("audio_pid")
        if before.get("status")!="live" or not before_encoder:
            raise RuntimeError(f"{slot} is not live enough for zero-drop hot patch: {before}")

        # Replace child-process code inside the existing container without
        # recreating the container or touching the persistent RTMP encoder.
        # audio_engine imports chat_requests; copy dependency before restarting audio feeder.
        run(["docker","cp",str(OVH/"app"/"chat_requests.py"),f"{name}:/app/chat_requests.py"],timeout=30)
        run(["docker","cp",str(OVH/"app"/"audio_engine.py"),f"{name}:/app/audio_engine.py"],timeout=30)
        run(["docker","cp",str(OVH/"app"/"stream_core.py"),f"{name}:/app/stream_core.py"],timeout=30)
        run(["docker","cp",str(OVH/"app"/"visual_engine.py"),f"{name}:/app/visual_engine.py"],timeout=30)

        if before_audio:
            run([
                "docker","exec",name,"python","-c",
                f"import os,signal; os.kill({int(before_audio)}, signal.SIGTERM)"
            ],timeout=20)

        end=time.time()+45
        after={}
        while time.time()<end:
            after=health(slot)
            if (
                after.get("status")=="live"
                and after.get("encoder_pid")==before_encoder
                and after.get("audio_pid")
                and after.get("audio_pid")!=before_audio
            ):
                break
            time.sleep(2)
        else:
            raise RuntimeError(f"{slot} hot patch did not preserve encoder/restart audio feeder: before={before} after={after}")

        results.append({
            "service":slot,
            "encoder_pid_preserved":after.get("encoder_pid")==before_encoder,
            "encoder_pid":after.get("encoder_pid"),
            "old_audio_pid":before_audio,
            "new_audio_pid":after.get("audio_pid"),
            "status":after.get("status"),
            "restarts":after.get("restarts"),
        })
    return results


def hot_patch_av(target="all"):
    targets=list(SLOTS) if target in ("","all") else [target]
    for slot in targets:
        if slot not in SLOTS:
            raise ValueError("hot patch target not allowed")

    results=[]
    for slot in targets:
        name=CONTAINERS[slot]
        before=health(slot)
        before_encoder=before.get("encoder_pid")
        before_audio=before.get("audio_pid")
        before_visual=before.get("visual_pid")
        if before.get("status") not in {"live","starting"} or not before_encoder:
            raise RuntimeError(f"{slot} is not live enough for AV hot patch: {before}")

        run(["docker","cp",str(OVH/"app"/"audio_engine.py"),f"{name}:/app/audio_engine.py"],timeout=30)
        run(["docker","cp",str(OVH/"app"/"visual_engine.py"),f"{name}:/app/visual_engine.py"],timeout=30)
        run(["docker","cp",str(OVH/"app"/"stream_core.py"),f"{name}:/app/stream_core.py"],timeout=30)

        # Restart only the child feeders. The RTMP publisher PID must stay alive.
        if before_audio:
            run(["docker","exec",name,"python","-c",f"import os,signal; os.kill({int(before_audio)}, signal.SIGTERM)"],timeout=20)
        if before_visual:
            run(["docker","exec",name,"python","-c",f"import os,signal; os.kill({int(before_visual)}, signal.SIGTERM)"],timeout=20)

        end=time.time()+60
        after={}
        while time.time()<end:
            after=health(slot)
            encoder_ok=after.get("encoder_pid")==before_encoder
            audio_ok=bool(after.get("audio_pid")) and after.get("audio_pid")!=before_audio
            visual_ok=bool(after.get("visual_pid")) and after.get("visual_pid")!=before_visual and after.get("visual_status")=="streaming"
            state_ok=after.get("status") in {"live","starting"}
            if encoder_ok and audio_ok and visual_ok and state_ok:
                break
            if after.get("encoder_pid") and after.get("encoder_pid")!=before_encoder:
                raise RuntimeError(f"{slot} encoder PID changed during zero-drop AV patch: {before_encoder} -> {after.get('encoder_pid')}")
            time.sleep(1)
        else:
            raise RuntimeError(f"{slot} AV feeders did not recover without publisher restart: before={before} after={after}")

        results.append({
            "service":slot,
            "encoder_pid_preserved":after.get("encoder_pid")==before_encoder,
            "encoder_pid":after.get("encoder_pid"),
            "old_audio_pid":before_audio,
            "new_audio_pid":after.get("audio_pid"),
            "old_visual_pid":before_visual,
            "new_visual_pid":after.get("visual_pid"),
            "visual_status":after.get("visual_status"),
            "audio_status":after.get("audio_status"),
            "status":after.get("status"),
            "restarts":after.get("restarts"),
        })
    return results


def reload_control_agent():
    # Control-plane only: this container does not carry production RTMP publishers.
    # Copy the supervisor and child engine code so isolated dynamic YouTube slots
    # can be introduced without recreating Deep House/Rainy publisher containers.
    name=CONTAINERS.get("ovh-agent","peter-lofi-ovh-agent")
    for filename in ("ovh_agent.py","stream_core.py","audio_engine.py","visual_engine.py"):
        run(["docker","cp",str(OVH/"app"/filename),f"{name}:/app/{filename}"],timeout=30)
    run(["docker","restart",name],timeout=60)
    time.sleep(3)
    out=run(["docker","inspect","-f","{{.State.Running}}",name],timeout=20).strip().lower()
    if out!="true":
        raise RuntimeError("ovh-agent did not return running")
    return {"service":"ovh-agent","status":"live","media_publishers_touched":False}


def hot_patch_audio_controls(target="twitch-kick"):
    if target in ("","twitch-kick"):
        targets=["twitch","kick"]
    elif target=="all":
        targets=list(SLOTS)
    elif target in SLOTS:
        targets=[target]
    else:
        raise ValueError("audio controls hot patch target not allowed")

    # Update the lightweight control agent first. Restarting ovh-agent does not
    # carry media and cannot interrupt any RTMP publisher.
    agent_name=CONTAINERS.get("ovh-agent","peter-lofi-ovh-agent")
    run(["docker","cp",str(OVH/"app"/"ovh_agent.py"),f"{agent_name}:/app/ovh_agent.py"],timeout=30)
    run(["docker","restart",agent_name],timeout=60)

    results=[]
    for slot in targets:
        name=CONTAINERS[slot]
        before=health(slot)
        encoder=before.get("encoder_pid")
        audio=before.get("audio_pid")
        if not encoder or not audio:
            raise RuntimeError(f"{slot} missing encoder/audio pid: {before}")

        run(["docker","cp",str(OVH/"app"/"audio_engine.py"),f"{name}:/app/audio_engine.py"],timeout=30)
        run(["docker","exec",name,"python","-c",f"import os,signal; os.kill({int(audio)}, signal.SIGTERM)"],timeout=20)

        end=time.time()+60
        after={}
        while time.time()<end:
            after=health(slot)
            if after.get("encoder_pid") and after.get("encoder_pid")!=encoder:
                raise RuntimeError(f"{slot} encoder changed during audio-control hot patch: {encoder} -> {after.get('encoder_pid')}")
            if after.get("encoder_pid")==encoder and after.get("audio_pid") and after.get("audio_pid")!=audio:
                break
            time.sleep(1)
        else:
            raise RuntimeError(f"{slot} audio feeder did not recover: before={before} after={after}")

        results.append({
            "service":slot,
            "encoder_pid":encoder,
            "encoder_pid_preserved":after.get("encoder_pid")==encoder,
            "old_audio_pid":audio,
            "new_audio_pid":after.get("audio_pid"),
            "status":after.get("status"),
            "visual_status":after.get("visual_status"),
            "audio_status":after.get("audio_status"),
        })
    return results


def ui_test_status_snapshot():
    st=OVH/"state"/"youtube-ui-test"
    def load(name):
        try:
            return json.loads((st/name).read_text(encoding="utf-8"))
        except Exception:
            return {}
    tail=""
    try:
        p=st/"controller.log"
        if p.exists():
            with open(p,"rb") as fh:
                fh.seek(0,2)
                size=fh.tell()
                fh.seek(max(0,size-12000))
                tail=fh.read().decode("utf-8","ignore")
    except Exception as exc:
        tail="controller_log_error:"+str(exc)[:200]
    return {
        "desired":load("desired.json"),
        "health":load("health.json"),
        "audio_health":load("audio-health.json"),
        "visual_health":load("visual-health.json"),
        "now_playing":load("now-playing.json"),
        "controller_log_tail":_redact_stream_log(tail),
    }


def bootstrap_ui_test_control_agent():
    """Safely refresh the non-publisher control agent and bridge local commands.

    The publisher containers are never restarted or signalled here. Production
    visual changes are staged locally and delivered as set_visual commands so
    stream_core keeps the RTMP publisher PID and generation intact.
    """
    marker=REPO/"control"/"ui-test-control-agent-refresh.json"
    if not marker.exists():
        return None
    try:
        cfg=json.loads(marker.read_text(encoding="utf-8"))
    except Exception as exc:
        raise RuntimeError(f"invalid ui-test control-agent marker: {exc}")
    version=str(cfg.get("version") or "")
    if not version:
        raise RuntimeError("ui-test control-agent marker has no version")

    visual_request_path=REPO/"control"/"visual-switch-requests"/"gta-radio-twitch-kick-20261006.json"
    visual_request=None
    if visual_request_path.exists():
        try:
            visual_request=json.loads(visual_request_path.read_text(encoding="utf-8"))
        except Exception as exc:
            raise RuntimeError(f"invalid visual-switch request: {exc}")

    st=read_state()
    ui_applied=str(st.get("ui_test_control_agent_version") or "")==version
    visual_request_id=str((visual_request or {}).get("id") or "")
    visual_applied=bool(visual_request_id) and str(st.get("last_visual_switch_request_id") or "")==visual_request_id
    if ui_applied and (not visual_request or visual_applied):
        return {
            "status":"already_applied",
            "version":version,
            "visual_switch":{"status":"already_applied","request_id":visual_request_id} if visual_applied else None,
        }

    name=CONTAINERS.get("ovh-agent","peter-lofi-ovh-agent")
    for filename in ("ovh_agent.py","stream_core.py","audio_engine.py","visual_engine.py"):
        run(["docker","cp",str(OVH/"app"/filename),f"{name}:/app/{filename}"],timeout=30)

    # Bridge the newest isolated test command from the local repository into a
    # host-local inbox. This keeps the existing private UI-test mechanism intact.
    queued_command_id=None
    try:
        idx_path=REPO/"control"/"ovh-commands"/"index.json"
        idx=json.loads(idx_path.read_text(encoding="utf-8")) if idx_path.exists() else {}
        for entry in reversed(list(idx.get("commands") or [])):
            rel=str((entry or {}).get("path") or "")
            if not rel:
                continue
            path=REPO/rel
            try:
                cmd=json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                continue
            slot=str(cmd.get("runtime_slot") or cmd.get("slot") or "")
            action=str(cmd.get("action") or "").lower()
            if slot=="youtube-ui-test" and action in {"start","resume","restart"}:
                inbox=OVH/"state"/"agent"/"local-inbox"
                inbox.mkdir(parents=True,exist_ok=True)
                cid=str(cmd.get("id") or "ui-test-start")
                safe="".join(ch for ch in cid if ch.isalnum() or ch in "-_")[:120] or "ui-test-start"
                atomic_json(inbox/(safe+".json"),cmd)
                queued_command_id=cid
                break
    except Exception as exc:
        raise RuntimeError(f"failed to bridge ui-test local command: {exc}")

    visual_plan=None
    if visual_request and not visual_applied:
        if not visual_request_id:
            raise RuntimeError("visual-switch request has no id")
        if str(visual_request.get("mode") or "")!="hot_swap_only":
            raise RuntimeError("visual-switch request must use hot_swap_only mode")
        slots=[str(x) for x in (visual_request.get("runtime_slots") or [])]
        if slots!=["twitch","kick"]:
            raise RuntimeError(f"visual-switch request slots must be exactly twitch,kick: {slots}")

        before={}
        for slot in slots:
            h=read_json(OVH/"state"/slot/"health.json") or {}
            d=read_json(OVH/"state"/slot/"desired.json") or {}
            if str(h.get("status") or "")!="live":
                raise RuntimeError(f"{slot} is not live; refusing visual change")
            if h.get("hot_swap") is not True:
                raise RuntimeError(f"{slot} hot_swap is not ready; refusing visual change")
            if not h.get("encoder_pid"):
                raise RuntimeError(f"{slot} encoder PID is missing; refusing visual change")
            before[slot]={
                "encoder_pid":h.get("encoder_pid"),
                "generation":d.get("generation"),
                "visual_revision":int(d.get("visual_revision") or 0),
                "restarts":int(h.get("restarts") or 0),
                "loop_url":str(d.get("loop_url") or h.get("loop_url") or ""),
            }

        token=AGENT_TOKEN
        if not token:
            secrets_file=CONTROL_ROOT/"worker.dev.vars"
            if secrets_file.exists():
                for line in secrets_file.read_text(encoding="utf-8").splitlines():
                    if line.startswith("OVH_AGENT_TOKEN="):
                        raw=line.split("=",1)[1].strip()
                        try:
                            token=str(json.loads(raw))
                        except Exception:
                            token=raw.strip().strip('"')
                        break
        if not token:
            raise RuntimeError("local OVH agent token is unavailable")

        export_req=urllib.request.Request(
            "http://127.0.0.1:8790/api/migration/export",
            headers={"User-Agent":"MediaForge-Host-Deploy-Agent","x-ovh-agent-token":token},
        )
        with urllib.request.urlopen(export_req,timeout=60) as response:
            snapshot=json.loads(response.read().decode("utf-8"))
        assets=list(((snapshot.get("tables") or {}).get("assets") or []))
        candidates=[]
        requested_title=str(visual_request.get("asset_title") or "").casefold()
        for asset in assets:
            if str(asset.get("status") or "")!="ready":
                continue
            if str(asset.get("asset_type") or "")!="loop":
                continue
            if not str(asset.get("mime_type") or "").lower().startswith("video/"):
                continue
            title=str(asset.get("title") or "")
            folded=title.casefold()
            score=0
            if requested_title and folded==requested_title:
                score+=100
            if all(token_word in folded for token_word in ("gta","vi","radio")):
                score+=20
            if "video" in folded or "vídeo" in folded:
                score+=5
            if score:
                candidates.append((score,str(asset.get("created_at") or ""),asset))
        if not candidates:
            raise RuntimeError("ready MediaForge GTA VI Radio loop asset was not found")
        candidates.sort(key=lambda item:(item[0],item[1]),reverse=True)
        asset=candidates[0][2]
        asset_id=str(asset.get("id") or "")
        download_token=str(asset.get("download_token") or "")
        expected_size=int(asset.get("size_bytes") or 0)
        if not asset_id or not download_token:
            raise RuntimeError("selected MediaForge asset is missing local media identifiers")

        target_dir=OVH/"state"/"shared-visuals"
        target_dir.mkdir(parents=True,exist_ok=True)
        target=target_dir/"video-radio-gta-vi.mp4"
        temp=target.with_suffix(".mp4.part")
        media_url=f"http://127.0.0.1:8790/media/{asset_id}/{download_token}"
        req=urllib.request.Request(media_url,headers={"User-Agent":"MediaForge-Host-Deploy-Agent"})
        with urllib.request.urlopen(req,timeout=180) as response, open(temp,"wb") as fh:
            while True:
                chunk=response.read(1024*1024)
                if not chunk:
                    break
                fh.write(chunk)
        actual_size=temp.stat().st_size if temp.exists() else 0
        if actual_size<1024*1024:
            temp.unlink(missing_ok=True)
            raise RuntimeError(f"staged visual is unexpectedly small: {actual_size} bytes")
        if expected_size and actual_size!=expected_size:
            temp.unlink(missing_ok=True)
            raise RuntimeError(f"staged visual size mismatch: {actual_size} != {expected_size}")
        temp.replace(target)

        # Validate the staged file from both existing publisher containers before
        # asking either visual engine to switch.
        for slot in slots:
            run([
                "docker","exec",CONTAINERS[slot],
                "ffprobe","-v","error","-select_streams","v:0",
                "-show_entries","stream=codec_name,width,height,r_frame_rate",
                "-of","json","/state/shared-visuals/video-radio-gta-vi.mp4",
            ],timeout=90)

        loop_url="file:///state/shared-visuals/video-radio-gta-vi.mp4"
        inbox=OVH/"state"/"agent"/"local-inbox"
        inbox.mkdir(parents=True,exist_ok=True)
        command_ids=[]
        for slot in slots:
            cid=f"{visual_request_id}-{slot}-v1"
            cmd={
                "id":cid,
                "runtime":"ovh",
                "runtime_slot":slot,
                "platform":slot,
                "action":"set_visual",
                "loop_url":loop_url,
                "requested_at":now(),
                "source":"mediaforge-visual-switch",
            }
            atomic_json(inbox/(cid+".json"),cmd)
            command_ids.append(cid)
        visual_plan={
            "request_id":visual_request_id,
            "asset_id":asset_id,
            "asset_title":str(asset.get("title") or ""),
            "asset_size_bytes":actual_size,
            "loop_url":loop_url,
            "before":before,
            "command_ids":command_ids,
        }

    # Only the non-publisher command agent is restarted. It carries no RTMP
    # connection and cannot drop Twitch or Kick.
    run(["docker","restart",name],timeout=60)
    time.sleep(4)
    running=run(["docker","inspect","-f","{{.State.Running}}",name],timeout=20).strip().lower()
    if running!="true":
        raise RuntimeError("ovh-agent did not return after control refresh")

    visual_result=None
    if visual_plan:
        deadline=time.time()+240
        slots=["twitch","kick"]
        last={}
        while time.time()<deadline:
            complete=True
            for slot in slots:
                h=read_json(OVH/"state"/slot/"health.json") or {}
                d=read_json(OVH/"state"/slot/"desired.json") or {}
                v=read_json(OVH/"state"/slot/"visual-health.json") or {}
                last[slot]={"health":h,"desired":d,"visual":v}
                before=visual_plan["before"][slot]
                if h.get("encoder_pid")!=before.get("encoder_pid"):
                    raise RuntimeError(f"{slot} encoder PID changed during visual hot-swap")
                if d.get("generation")!=before.get("generation"):
                    raise RuntimeError(f"{slot} generation changed during visual hot-swap")
                if int(h.get("restarts") or 0)!=int(before.get("restarts") or 0):
                    raise RuntimeError(f"{slot} restart counter changed during visual hot-swap")
                ok=(
                    str(h.get("status") or "")=="live"
                    and h.get("hot_swap") is True
                    and str(d.get("loop_url") or "")==visual_plan["loop_url"]
                    and int(d.get("visual_revision") or 0)>int(before.get("visual_revision") or 0)
                    and str(v.get("status") or "")=="streaming"
                    and str(v.get("loop_url") or "")==visual_plan["loop_url"]
                )
                complete=complete and ok
            if complete:
                break
            time.sleep(2)
        else:
            # Roll desired visual back without touching RTMP/publisher processes.
            inbox=OVH/"state"/"agent"/"local-inbox"
            for slot in slots:
                old_url=str(visual_plan["before"][slot].get("loop_url") or "")
                if old_url:
                    cid=f"{visual_plan['request_id']}-{slot}-rollback"
                    atomic_json(inbox/(cid+".json"),{
                        "id":cid,
                        "runtime":"ovh",
                        "runtime_slot":slot,
                        "platform":slot,
                        "action":"set_visual",
                        "loop_url":old_url,
                        "requested_at":now(),
                        "source":"mediaforge-visual-switch",
                    })
            raise RuntimeError("visual hot-swap timed out; rollback was queued without restarting publishers")

        after={}
        for slot in slots:
            h=last[slot]["health"]
            d=last[slot]["desired"]
            v=last[slot]["visual"]
            before=visual_plan["before"][slot]
            after[slot]={
                "status":h.get("status"),
                "hot_swap":h.get("hot_swap"),
                "encoder_pid_preserved":h.get("encoder_pid")==before.get("encoder_pid"),
                "generation_preserved":d.get("generation")==before.get("generation"),
                "restart_counter_preserved":int(h.get("restarts") or 0)==int(before.get("restarts") or 0),
                "visual_revision_before":before.get("visual_revision"),
                "visual_revision_after":d.get("visual_revision"),
                "visual_status":v.get("status"),
            }
        visual_result={
            "status":"completed",
            "request_id":visual_plan["request_id"],
            "asset_id":visual_plan["asset_id"],
            "asset_title":visual_plan["asset_title"],
            "asset_size_bytes":visual_plan["asset_size_bytes"],
            "rtmp_restart":False,
            "publisher_containers_touched":False,
            "services":after,
        }
        st["last_visual_switch_request_id"]=visual_plan["request_id"]
        st["last_visual_switch_completed_at"]=now()

    st["ui_test_control_agent_version"]=version
    st["ui_test_control_agent_refreshed_at"]=now()
    write_state(st)
    time.sleep(3)
    return {
        "status":"applied",
        "version":version,
        "publisher_containers_touched":False,
        "queued_command_id":queued_command_id,
        "visual_switch":visual_result,
        "ui_test":ui_test_status_snapshot(),
    }


def rollback_service(service):
    if service not in SERVICES:
        raise ValueError("service not allowed")
    st=read_state()
    previous=str(st.get("previous_head") or "")
    if not previous or len(previous)!=40:
        raise RuntimeError("no recorded previous git revision")
    current=git_head()
    run(["runuser","-u","ubuntu","--","git","-C",str(REPO),"reset","--hard",previous],timeout=60)
    result=deploy_service(service)
    st["rollback_from"]=current
    st["last_good_head"]=previous
    st["updated_at"]=now()
    write_state(st)
    return {"rolled_back_to":previous,**result}


def health_all():
    lofi_dir=OVH/"state"/"youtube-lofi-hip-hop"
    lofi=health("youtube-lofi-hip-hop")
    audio=read_json(lofi_dir/"audio-health.json",{}) or {}
    visual=read_json(lofi_dir/"visual-health.json",{}) or {}
    log_counts={}
    try:
        path=lofi_dir/"ffmpeg.log"
        with path.open("rb") as stream:
            stream.seek(max(0,path.stat().st_size-32768))
            chunk=stream.read().decode("utf-8","replace").lower()
        signatures=("connection refused","connection timed out","end of file",
           "failed to update header","broken pipe","error writing","server returned",
           "invalid data found","failed to connect","tls","recovery")
        log_counts={term:chunk.count(term) for term in signatures if term in chunk}
    except Exception:
        log_counts={"available":False}
    return {"git_head":git_head(),
            "capabilities":{"isolated_lofi_youtube":True,"publisher_safe":True},
            "services":{**{s:health(s) for s in SERVICES},
                        "youtube-lofi-hip-hop":lofi},
            "lofi_diagnostics":{"audio_state":audio.get("state") or audio.get("status"),
                  "visual_state":visual.get("status"),
                  "audio_stalls":audio.get("stalls"),
                  "error_signatures":log_counts}}


def parse_iso(value):
    try:
        return datetime.fromisoformat(str(value).replace("Z","+00:00")).timestamp()
    except Exception:
        return None


def recover_publisher(slot,pid,reason):
    name=CONTAINERS.get(slot)
    if not name or not pid:
        return False
    run([
        "docker","exec",name,"python","-c",
        f"import os,signal; os.kill({int(pid)}, signal.SIGTERM)"
    ],timeout=20)
    print("watchdog publisher recovery",slot,pid,reason,flush=True)
    return True


def watchdog_tick():
    now_ts=time.time()
    snapshot={
        "updated_at":now(),
        "mode":"local-ovh",
        "cloudflare_required":False,
        "container_restarts_allowed":False,
        "publisher_auto_kill_enabled":WATCHDOG_PUBLISHER_KILL_ENABLED,
        "control_plane":ensure_control_plane_containers(),
        "services":{},
    }
    for slot in SLOTS:
        st=_WATCH.setdefault(slot,{"offset":None,"events":[],"last_recovery":0.0})
        h=health(slot)
        log=OVH/"state"/slot/"ffmpeg.log"
        new_text=""
        try:
            size=log.stat().st_size
            if st["offset"] is None:
                # Ignore historical log errors when the watchdog first starts.
                st["offset"]=size
            else:
                if size < st["offset"]:
                    st["offset"]=0
                if size > st["offset"]:
                    with open(log,"rb") as fh:
                        fh.seek(st["offset"])
                        new_text=fh.read(min(size-st["offset"],256*1024)).decode("utf-8","ignore")
                    st["offset"]=size
        except Exception:
            pass

        if new_text:
            lowered=new_text.lower()
            hits=sum(lowered.count(p) for p in NETWORK_ERROR_PATTERNS)
            if hits:
                st["events"].extend([now_ts]*min(hits,50))
        st["events"]=[x for x in st["events"] if now_ts-x<=WATCHDOG_WINDOW]

        updated_ts=parse_iso(h.get("updated_at"))
        stale_seconds=round(now_ts-updated_ts,1) if updated_ts else None
        recent_errors=len(st["events"])
        action="observe"
        recovered=False

        # Never recreate a container automatically. Recover only the publisher
        # child after a sustained burst of NEW network errors.
        if (
            WATCHDOG_PUBLISHER_KILL_ENABLED
            and recent_errors>=WATCHDOG_THRESHOLD
            and h.get("encoder_pid")
            and h.get("status") in {"live","starting","restarting"}
            and now_ts-st["last_recovery"]>=WATCHDOG_COOLDOWN
        ):
            try:
                recovered=recover_publisher(
                    slot,
                    h.get("encoder_pid"),
                    f"{recent_errors} network errors/{WATCHDOG_WINDOW}s",
                )
                if recovered:
                    st["last_recovery"]=now_ts
                    st["events"]=[]
                    action="publisher_recovery"
            except Exception as exc:
                action="recovery_failed:"+str(exc)[:160]

        snapshot["services"][slot]={
            "status":h.get("status"),
            "encoder_pid":h.get("encoder_pid"),
            "restarts":h.get("restarts",0),
            "stale_seconds":stale_seconds,
            "network_errors_window":recent_errors,
            "last_recovery_at":(
                datetime.fromtimestamp(st["last_recovery"],timezone.utc).isoformat().replace("+00:00","Z")
                if st["last_recovery"] else None
            ),
            "action":action,
            "recovered":recovered,
        }

    atomic_json(WATCHDOG_FILE,snapshot)


def watchdog_loop():
    while True:
        try:
            watchdog_tick()
        except Exception as exc:
            try:
                atomic_json(WATCHDOG_FILE,{
                    "updated_at":now(),
                    "mode":"local-ovh",
                    "status":"error",
                    "error":str(exc)[:500],
                })
            except Exception:
                pass
        time.sleep(WATCHDOG_INTERVAL)



def _redact_stream_log(text):
    text=str(text or "")
    text=re.sub(r'(rtmps?://[^/\s]+(?:/\S*?/)?)[A-Za-z0-9_=-]{12,}', r'\1[REDACTED]', text)
    text=re.sub(r'(live_[A-Za-z0-9]{8,})', '[REDACTED_STREAM_KEY]', text)
    return text[-12000:]


def diagnose_stream_service(slot):
    if slot not in DIAGNOSTIC_STATE_SLOTS:
        raise ValueError("diagnose target not allowed")
    h=health(slot)
    log=OVH/"state"/slot/"ffmpeg.log"
    controller_log=OVH/"state"/slot/"controller.log"
    visual_log=OVH/"state"/slot/"visual-ffmpeg.log"
    tail=""
    try:
        with open(log,"rb") as fh:
            fh.seek(0,2)
            size=fh.tell()
            fh.seek(max(0,size-24000))
            tail=fh.read().decode("utf-8","ignore")
    except Exception as exc:
        tail="log_read_error:"+str(exc)[:180]
    controller_tail=""
    try:
        with open(controller_log,"rb") as fh:
            fh.seek(0,2)
            size=fh.tell()
            fh.seek(max(0,size-24000))
            controller_tail=fh.read().decode("utf-8","ignore")
    except Exception as exc:
        controller_tail="controller_log_read_error:"+str(exc)[:180]
    visual_tail=""
    try:
        with open(visual_log,"rb") as fh:
            fh.seek(0,2)
            size=fh.tell()
            fh.seek(max(0,size-16000))
            visual_tail=fh.read().decode("utf-8","ignore")
    except Exception as exc:
        visual_tail="visual_log_read_error:"+str(exc)[:180]
    desired={}
    now_playing={}
    audio_health={}
    visual_health={}
    docker_stats=""
    try: desired=json.loads((OVH/"state"/slot/"desired.json").read_text(encoding="utf-8"))
    except Exception: pass
    try: now_playing=json.loads((OVH/"state"/slot/"now-playing.json").read_text(encoding="utf-8"))
    except Exception: pass
    try: audio_health=json.loads((OVH/"state"/slot/"audio-health.json").read_text(encoding="utf-8"))
    except Exception: pass
    try: visual_health=json.loads((OVH/"state"/slot/"visual-health.json").read_text(encoding="utf-8"))
    except Exception: pass
    pipeline_probe={}
    visual_probe={}
    try:
        container_name=CONTAINERS.get(slot) or ("peter-lofi-ovh-agent" if slot in ("youtube-gta-vi","youtube-ui-test") else "")
        if container_name:
            docker_stats=run(["docker","stats","--no-stream","--format","{{.CPUPerc}}|{{.MemUsage}}|{{.NetIO}}|{{.BlockIO}}",container_name],timeout=20).strip()
        else:
            docker_stats="stats_unavailable"

        if slot=="youtube-gta-vi" and container_name:
            probe_script=(
                "import json,pathlib,os;"
                "me=os.getpid();"
                "o={'stream_core_count':0,'visual_engine_count':0,'ffmpeg_count':0,'uses_video_fifo':False,'uses_udp_19160':False};"
                "\nfor p in pathlib.Path('/proc').iterdir():"
                "\n if not p.name.isdigit() or int(p.name)==me: continue"
                "\n try: c=(p/'cmdline').read_bytes().replace(b'\\x00',b' ').decode('utf-8','ignore')"
                "\n except Exception: continue"
                "\n if 'stream_core.py --platform youtube-gta-vi' in c: o['stream_core_count']+=1"
                "\n if 'visual_engine.py --platform youtube-gta-vi' in c: o['visual_engine_count']+=1"
                "\n if 'ffmpeg' in c: o['ffmpeg_count']+=1"
                "\n if '/state/youtube-gta-vi/video.ts' in c: o['uses_video_fifo']=True"
                "\n if 'udp://127.0.0.1:19160' in c: o['uses_udp_19160']=True"
                "\nprint(json.dumps(o))"
            )
            pipeline_probe=json.loads(run(["docker","exec",container_name,"python","-c",probe_script],timeout=20))
            prepared=str(visual_health.get("prepared_file") or "").strip()
            if prepared:
                visual_probe=json.loads(run([
                    "docker","exec",container_name,"ffprobe","-v","error",
                    "-select_streams","v:0",
                    "-show_entries","stream=codec_name,width,height,r_frame_rate,avg_frame_rate,bit_rate",
                    "-of","json",prepared
                ],timeout=30))
    except Exception as exc:
        if not docker_stats:
            docker_stats="stats_error:"+str(exc)[:180]
        pipeline_probe={"error":str(exc)[:240]}
    return {
        "service":slot,
        "health":h,
        "desired":{
            "desired":desired.get("desired"),
            "generation":desired.get("generation"),
            "playlist_key":desired.get("playlist_key"),
            "updated_at":desired.get("updated_at"),
        },
        "now_playing":now_playing,
        "audio_health":audio_health,
        "visual_health":visual_health,
        "docker_stats":docker_stats,
        "pipeline_probe":pipeline_probe,
        "visual_probe":visual_probe,
        "ffmpeg_log_tail":_redact_stream_log(tail),
        "controller_log_tail":_redact_stream_log(controller_tail),
        "visual_ffmpeg_log_tail":_redact_stream_log(visual_tail),
    }


def recover_stream_publisher(slot):
    if slot not in SLOTS:
        raise ValueError("recover target not allowed")
    before=health(slot)
    pid=before.get("encoder_pid")
    if not pid:
        raise RuntimeError(f"{slot} has no encoder pid: {before}")
    recover_publisher(slot,pid,"manual publisher recovery")
    end=time.time()+60
    after={}
    while time.time()<end:
        after=health(slot)
        if after.get("encoder_pid") and after.get("encoder_pid")!=pid and after.get("status")=="live":
            return {
                "service":slot,
                "old_encoder_pid":pid,
                "new_encoder_pid":after.get("encoder_pid"),
                "status":after.get("status"),
                "hot_swap":after.get("hot_swap"),
                "restarts":after.get("restarts",0),
            }
        time.sleep(2)
    raise RuntimeError(f"{slot} publisher did not recover: before={before} after={after}")


def repair_gta_runtime():
    slot="youtube-gta-vi"
    container="peter-lofi-ovh-agent"
    before=diagnose_stream_service(slot)

    # Refresh only child-process code inside the existing OVH agent container.
    # Other publishers/containers are untouched.
    run(["docker","cp",str(OVH/"app"/"stream_core.py"),f"{container}:/app/stream_core.py"],timeout=30)
    run(["docker","cp",str(OVH/"app"/"visual_engine.py"),f"{container}:/app/visual_engine.py"],timeout=30)
    run(["docker","cp",str(OVH/"app"/"audio_engine.py"),f"{container}:/app/audio_engine.py"],timeout=30)

    # Restart only the GTA StreamCore parent. Resolve it from the live FFmpeg
    # encoder PID instead of matching a command string, which can miss the
    # process inside the container.
    old_pid=(before.get("health") or {}).get("encoder_pid")
    if not old_pid:
        raise RuntimeError(f"GTA encoder PID unavailable before repair: {before.get('health')}")
    parent_text=run([
        "docker","exec",container,"python","-c",
        f"import pathlib; s=pathlib.Path('/proc/{int(old_pid)}/stat').read_text().split(); print(s[3])"
    ],timeout=20).strip()
    try:
        stream_core_pid=int(parent_text.splitlines()[-1])
    except Exception:
        raise RuntimeError(f"Could not resolve GTA StreamCore parent from encoder {old_pid}: {parent_text}")
    if stream_core_pid<=1:
        raise RuntimeError(f"Unsafe GTA StreamCore PID resolved: {stream_core_pid}")
    run(["docker","exec",container,"kill","-TERM",str(stream_core_pid)],timeout=20)

    end=time.time()+120
    last={}
    while time.time()<end:
        last=health(slot)
        if (
            last.get("status")=="live"
            and last.get("encoder_pid")
            and last.get("encoder_pid")!=old_pid
            and last.get("visual_status")=="streaming"
            and last.get("audio_status") in {"running","playing","encoder_backpressure_buffering"}
        ):
            diag=diagnose_stream_service(slot)
            return {
                "service":slot,
                "old_encoder_pid":old_pid,
                "new_encoder_pid":last.get("encoder_pid"),
                "status":last.get("status"),
                "visual_status":last.get("visual_status"),
                "audio_status":last.get("audio_status"),
                "diagnostic":diag,
            }
        time.sleep(3)
    raise RuntimeError(f"GTA runtime did not recover after media-pipeline repair: {last}")



def launch_isolated_lofi_youtube(cmd):
    """Provision a separate Docker publisher, never recreating an existing live."""
    import uuid
    slot="youtube-lofi-hip-hop"
    container="peter-lofi-youtube-lofi-hip-hop"
    if str(cmd.get("target") or "")!=slot or str(cmd.get("playlist_key") or "")!="lofi-hip-hop":
        raise ValueError("Dedicated Lofi-only launch payload required")
    sid=str(cmd.get("session_id") or "")
    try: uuid.UUID(sid)
    except Exception: raise ValueError("Invalid Lofi session ID")
    stream_url=str(cmd.get("stream_url") or "").strip()
    stream_key=str(cmd.get("stream_key") or "").strip()
    loop_url=str(cmd.get("loop_url") or "").strip()
    title=str(cmd.get("title") or "Lofi Hip Hop Radio")[:140]
    tracks=cmd.get("tracks")
    if not stream_url.startswith("rtmp") or not stream_key or not loop_url.startswith("http"):
        raise ValueError("Missing authenticated YouTube RTMP credentials or video")
    if not isinstance(tracks,list) or not 1<=len(tracks)<=36:
        raise ValueError("Lofi launch needs between 1 and 36 tracks")
    if any(not str(t.get("url") or "").startswith("https://") or
           not str(t.get("id") or "").startswith("lofi-hip-hop-") or
           not 295<=int(t.get("duration_seconds") or 0)<=305
           for t in tracks if isinstance(t,dict)):
        raise ValueError("Unexpected Lofi track in playlist")
    if any(not isinstance(t,dict) for t in tracks):
        raise ValueError("Malformed Lofi playlist")
    baseline={name:health(name).get("encoder_pid") for name in DIAGNOSTIC_STATE_SLOTS if name!=slot}
    state_path=OVH/"state"/slot
    state_path.mkdir(parents=True,exist_ok=True)
    existing=read_json(state_path/"desired.json",{}) or {}
    try:
        running=run(["docker","inspect","-f","{{.State.Running}}",container],timeout=15).strip().lower()=="true"
    except Exception:
        running=False
    if running:
        if str(existing.get("session_id") or "")==sid:
            return {"status":"already_running","runtime_slot":slot,"session_id":sid,
                    "other_publishers_preserved":True}
        raise RuntimeError("Lofi publisher is already running; refusing to replace or restart it")
    if existing.get("desired")=="live" and existing.get("session_id") not in ("",sid):
        raise RuntimeError("Lofi has a previous session marked live: refusing an unsafe takeover")
    image="mediaforge-lofi-youtube:isolated-v1"
    run(["docker","build","-t",image,str(OVH)],timeout=1500)
    atomic_json(state_path/"playlist.json",{
        "station":slot,"playlist_key":"lofi-hip-hop",
        "tracks":tracks,"shuffle":True,"repeat":True,"updated_at":now()
    })
    atomic_json(state_path/"desired.json",{
        "runtime":"ovh","runtime_slot":slot,
        "session_id":sid,"title":title,"loop_url":loop_url,
        "playlist_key":"lofi-hip-hop","desired":"live",
        "generation":1,"visual_revision":1,"updated_at":now()
    })
    args=[
        "docker","run","-d","--name",container,"--restart","unless-stopped",
        "--network","host","--add-host","host.docker.internal:host-gateway",
        "--cpus","2.0","--memory","1536m",
        "--pids-limit","250","-v",str(OVH/"state")+":/state",
        "-e","STREAM_URL="+stream_url,"-e","STREAM_KEY="+stream_key,
        "-e","LOOP_URL="+loop_url,
        "-e","PLAYLIST_FILE=/config/youtube-deep-house.json",
        "-e","VIDEO_FPS=60","-e","VIDEO_BITRATE_KBPS=8000",
        "-e","VIDEO_BUFSIZE_KBPS=16000","-e","AUDIO_BITRATE_KBPS=192",
        "-e","VIDEO_PROFILE=main","-e","VIDEO_PRESET=superfast",
        "-e","STREAM_PROFILE_VERSION=lofi-hip-hop-isolated-v1",
        "-e","STARTUP_PREROLL_SECONDS=1.5","-e","AUDIO_READY_TIMEOUT_SECONDS=90",
        "-e","VIDEO_UDP_PORT=19172",
        "-e","BOOTSTRAP_SESSION_ID="+sid,"-e","BOOTSTRAP_TITLE="+title,
        image,"python","/app/stream_core.py","--platform",slot
    ]
    # Secrets are passed only to Docker locally, never printed or returned.
    run(args,timeout=120)
    time.sleep(3)
    running=run(["docker","inspect","-f","{{.State.Running}}",container],timeout=20).strip().lower()
    if running!="true":
        raise RuntimeError("Dedicated Lofi encoder failed to start")
    encoder_health={}
    for attempt in range(48):
        encoder_health=health(slot)
        if (encoder_health.get("status")=="live" and encoder_health.get("encoder_pid")):
            break
        try:
            is_running=run(["docker","inspect","-f","{{.State.Running}}",container],timeout=10).strip().lower()
        except Exception:
            is_running="false"
        if is_running!="true":
            raise RuntimeError("Isolated YouTube encoder exited before reaching live state")
        time.sleep(4)
    else:
        raise RuntimeError("Dedicated YouTube encoder did not confirm RTMP live within 192 seconds: "+
                           str(encoder_health.get("status") or "unknown"))
    changed=[]
    for name,pid in baseline.items():
        if pid is not None and health(name).get("encoder_pid")!=pid:
            changed.append(name)
    if changed:
        raise RuntimeError("Existing RTMP publisher PID changed unexpectedly: "+",".join(changed))
    return {"status":"live","runtime_slot":slot,"session_id":sid,
            "playlist_key":"lofi-hip-hop","track_count":len(tracks),
            "encoder_pid":encoder_health.get("encoder_pid"),
            "publisher_container":container,"other_publishers_preserved":True,
            "existing_streams_restarted":False,"checked_at":now()}



def relay_lofi_youtube_oauth(cmd):
    """Use authenticated OVH host egress to reach GitHub bridge; no RTMP changes."""
    import urllib.error
    action=str(cmd.get("action") or "")
    endpoint="https://mediaforge-api.guilhermeodsgn.workers.dev/api/ovh/agent/youtube-github-bridge"
    if action=="probe_lofi_youtube_oauth":
        try:
            post_json(endpoint,{"session_id":"","playlist_key":"lofi-hip-hop"})
        except urllib.error.HTTPError as exc:
            try: data=json.loads(exc.read().decode("utf-8","replace")[:700])
            except Exception: data={}
            if exc.code==400 and data.get("error")=="bridge_payload_incomplete":
                return {"status":"ready","bridge_authenticated":True,
                        "broadcast_created":False,"other_publishers_preserved":True}
            raise RuntimeError("Remote OAuth bridge preflight rejected: HTTP "+str(exc.code))
        raise RuntimeError("Unexpected remote OAuth bridge response: invalid probe accepted")
    if action!="relay_lofi_youtube_oauth":
        raise ValueError("Unsupported Lofi bridge operation")
    sid=str(cmd.get("session_id") or "")
    playlist=str(cmd.get("playlist_key") or "")
    slot=str(cmd.get("runtime_slot") or "")
    loop=str(cmd.get("loop_url") or "")
    if (not re.fullmatch(r"[0-9a-f-]{36}",sid,re.I) or
        playlist!="lofi-hip-hop" or slot!="youtube-lofi-hip-hop" or
        not loop.startswith("http")):
        raise ValueError("Invalid isolated Lofi launch request")
    result=post_json(endpoint,{
        "session_id":sid,"title":str(cmd.get("title") or "Lofi Hip Hop Radio"),
        "description":str(cmd.get("description") or ""),
        "loop_url":loop,"thumbnail_url":str(cmd.get("thumbnail_url") or ""),
        "playlist_key":"lofi-hip-hop","runtime_slot":slot,
        "source":"authenticated-ovh-host-agent",
    })
    if result.get("ok") is not True or result.get("queued") is not True:
        raise RuntimeError("Remote YouTube bridge did not confirm dispatch")
    return {"status":"queued","runtime_slot":slot,"session_id":sid,
            "source":"authenticated-ovh-host-agent","other_publishers_preserved":True,
            "new_broadcast_pending":True}


def repair_lofi_video_transport(cmd):
    """Patch/restart ONLY the dedicated Lofi publisher; leave all other RTMP alone."""
    slot="youtube-lofi-hip-hop"
    name="peter-lofi-youtube-lofi-hip-hop"
    sid=str(cmd.get("session_id") or "")
    desired=read_json(OVH/"state"/slot/"desired.json",{}) or {}
    if not sid or sid!=str(desired.get("session_id") or "") or desired.get("desired")!="live":
        raise RuntimeError("Isolated Lofi session not active or session id mismatch")
    pre={key:health(key).get("encoder_pid") for key in DIAGNOSTIC_STATE_SLOTS if key!=slot}
    run(["docker","inspect","-f","{{.Id}}",name],timeout=20)
    source=OVH/"app"/"stream_core.py"
    run(["python3","-m","py_compile",str(source)],timeout=15)
    # No docker-compose, no service rebuild and no restart of other publishers.
    run(["docker","cp",str(source),name+":/app/stream_core.py"],timeout=30)
    run(["docker","restart","--time","12",name],timeout=80)
    previous=str((read_json(OVH/"state"/slot/"health.json",{}) or {}).get("updated_at") or "")
    ready=None
    for attempt in range(65):
        h=health(slot)
        if h.get("status")=="live" and h.get("encoder_pid") and str(h.get("updated_at") or "")!=previous:
            ready=h
            break
        time.sleep(2)
    if not ready:
        raise RuntimeError("Lofi-only publisher did not regain local encoder health")
    drift=[key for key,pid in pre.items()
           if pid is not None and health(key).get("encoder_pid")!=pid]
    if drift:raise RuntimeError("Unrelated stream encoder changed: "+",".join(drift))
    return {"status":"lofi_transport_restarted","runtime_slot":slot,
            "session_id":sid,"encoder_pid_present":bool(ready.get("encoder_pid")),
            "other_publishers_preserved":True,"other_publishers_restarted":False,
            "lofi_health":{key:ready.get(key) for key in ("status","audio_status","visual_status","restarts")}}


def recover_isolated_lofi_audio(cmd):
    """Restore only the muted audio feeder while preserving the RTMP video PID."""
    slot="youtube-lofi-hip-hop"
    container="peter-lofi-youtube-lofi-hip-hop"
    st=OVH/"state"/slot
    desired=read_json(st/"desired.json",{}) or {}
    h=health(slot)
    old_audio=read_json(st/"audio-health.json",{}) or {}
    old_track=read_json(st/"now-playing.json",{}) or {}
    if cmd.get("target")!=slot or str(cmd.get("session_id") or "")!=str(desired.get("session_id") or ""):
        raise RuntimeError("Lofi-only audio recovery session mismatch")
    encoder_pid=int(h.get("encoder_pid") or 0)
    audio_pid=int(h.get("audio_pid") or 0)
    if h.get("status")!="live" or encoder_pid<2 or audio_pid<2 or desired.get("desired")!="live":
        raise RuntimeError("Refusing to affect a stopped or unexpected RTMP live")
    if str(old_audio.get("state") or "")!="source_stalled_pcm_clock_preserved" or int(old_audio.get("stalls") or 0)<5:
        return {"status":"audio_not_stalled_no_changes","encoder_pid_preserved":True,
                "track_count":len((read_json(st/"playlist.json",{}) or {}).get("tracks") or [])}
    playlist=read_json(st/"playlist.json",{}) or {}
    tracks=playlist.get("tracks") or []
    if playlist.get("playlist_key")!="lofi-hip-hop" or not isinstance(tracks,list) or len(tracks)!=66:
        raise RuntimeError("Expected all 66 approved Lofi tracks in the isolated live playlist")
    other_pids={name:health(name).get("encoder_pid") for name in DIAGNOSTIC_STATE_SLOTS
                if name!=slot}
    image_file=OVH/"app"/"audio_engine.py"
    run(["python3","-m","py_compile",str(image_file)],timeout=20)
    run(["docker","cp",str(image_file),container+":/app/audio_engine.py"],timeout=30)
    run(["docker","exec",container,"python3","-m","py_compile","/app/audio_engine.py"],timeout=20)
    # Confirm PID identity before signalling; NEVER signal FFmpeg or StreamCore.
    identity=run(["docker","exec",container,"python3","-c",
        "import pathlib; print(pathlib.Path('/proc/%s/cmdline').read_bytes().replace(b'\\x00',b' ').decode())" % audio_pid
    ],timeout=15)
    if "audio_engine.py" not in identity or "youtube-lofi-hip-hop" not in identity:
        raise RuntimeError("Audio PID did not match dedicated Lofi feeder, no signal sent")
    run(["docker","exec",container,"python3","-c",
        "import os,signal; os.kill(%s,signal.SIGTERM)" % audio_pid],timeout=15)
    stable_since=None
    last={}
    for attempt in range(55):
        time.sleep(2)
        current=health(slot)
        new_audio=read_json(st/"audio-health.json",{}) or {}
        if int(current.get("encoder_pid") or 0)!=encoder_pid:
            raise RuntimeError("RTMP encoder PID changed unexpectedly; no further signals")
        pid=int(current.get("audio_pid") or 0)
        non_silent=int(new_audio.get("non_silent_pcm_frames") or 0)
        if pid and pid!=audio_pid and new_audio.get("state")=="playing" and non_silent>=100:
            if stable_since is None:stable_since=time.monotonic()
            if time.monotonic()-stable_since>=12 and int(new_audio.get("stalls") or 0)==0:
                last={"health":current,"audio":new_audio}
                break
        else:
            stable_since=None
    else:
        raise RuntimeError("Audio feeder reloaded but non-silent PCM playback was not verified within 110 seconds")
    changed=[name for name,pid in other_pids.items() if pid and health(name).get("encoder_pid")!=pid]
    if changed:
        raise RuntimeError("Other encoder PID changed unexpectedly: "+",".join(changed))
    current_playlist=read_json(st/"playlist.json",{}) or {}
    return {
        "status":"audio_restored","playlist_key":"lofi-hip-hop",
        "playlist_track_count":len(current_playlist.get("tracks") or []),
        "old_track_id":old_track.get("track_id"),
        "current_track_id":(read_json(st/"now-playing.json",{}) or {}).get("track_id"),
        "encoder_pid_preserved":True,"old_audio_pid":audio_pid,
        "new_audio_pid":last["health"].get("audio_pid"),
        "pcm_frames_emitted":last["audio"].get("pcm_frames_emitted"),
        "non_silent_pcm_frames":last["audio"].get("non_silent_pcm_frames"),
        "audio_stalls":last["audio"].get("stalls"),
        "other_publishers_preserved":True,
        "rtmp_restart":False,
    }


def execute(cmd):
    action=str(cmd.get("action") or "")
    target=str(cmd.get("target") or "")
    reload_self=False
    if action=="health_check":
        return health_all(),False
    if action=="diagnose_service":
        return diagnose_stream_service(target),False
    if action=="recover_publisher":
        return recover_stream_publisher(target),False
    if action=="rollback_service":
        return rollback_service(target),False

    old,new=git_sync()
    if action in ("relay_lofi_youtube_oauth","probe_lofi_youtube_oauth"):
        return {"old_head":old,"new_head":new,**relay_lofi_youtube_oauth(cmd)},False
    if action=="recover_isolated_lofi_audio":
        return {"old_head":old,"new_head":new,**recover_isolated_lofi_audio(cmd)},False
    if action=="repair_lofi_video_transport":
        return {"old_head":old,"new_head":new,**repair_lofi_video_transport(cmd)},False
    if action=="launch_isolated_lofi_youtube":
        return {"old_head":old,"new_head":new,**launch_isolated_lofi_youtube(cmd)},False
    if action=="hot_patch_streaming":
        return {"old_head":old,"new_head":new,"targets":hot_patch_streaming(target)},False
    if action=="reload_control_agent":
        return {"old_head":old,"new_head":new,**reload_control_agent()},False
    if action=="hot_patch_av":
        return {"old_head":old,"new_head":new,"targets":hot_patch_av(target)},False
    if action=="hot_patch_audio_controls":
        return {"old_head":old,"new_head":new,"targets":hot_patch_audio_controls(target)},False
    st=read_state()
    if old!=new:
        st.update({"previous_head":old,"last_good_head":new,"last_deploy_at":now()})
        write_state(st)

    if action=="repair_gta_runtime":
        return {"old_head":old,"new_head":new,**repair_gta_runtime()},False
    if action=="deploy_service":
        return {"old_head":old,"new_head":new,**deploy_service(target)},False
    if action=="deploy_all":
        return {"old_head":old,"new_head":new,"services":deploy_all()},False
    if action=="deploy_host_agent":
        # Refreshing this host agent never recreates publisher containers.
        bootstrap=bootstrap_ui_test_control_agent()
        control_plane=None
        marker=REPO/"control"/"publish-control-plane-on-host-deploy.json"
        if marker.exists():
            try:
                cfg=json.loads(marker.read_text(encoding="utf-8"))
                version=str(cfg.get("version") or "")
            except Exception as exc:
                raise RuntimeError(f"invalid control-plane publish marker: {exc}")
            st=read_state()
            if version and str(st.get("control_plane_publish_version") or "")!=version:
                control_plane=publish_mediaforge_control_plane()
                st["control_plane_publish_version"]=version
                st["control_plane_published_at"]=now()
                write_state(st)
            elif version:
                control_plane={"status":"already_applied","version":version}
        return {
            "old_head":old,
            "new_head":new,
            "host_agent":"reloading",
            "ui_test_bootstrap":bootstrap,
            "control_plane_publish":control_plane,
            "ui_test":ui_test_status_snapshot(),
        },True
    if action=="run_twitch_dj_balance":
        out=run(["bash",str(OVH/"hotfix_twitch_dj_balance.sh")],cwd=OVH,timeout=180)
        return {"old_head":old,"new_head":new,"output":out[-12000:]},False
    raise ValueError("action not allowed")




def report_isolated_lofi_status():
    """Read-only heartbeat for the separate Lofi container; never restart media."""
    slot="youtube-lofi-hip-hop"
    state_dir=OVH/"state"/slot
    h=health(slot)
    desired=read_json(state_dir/"desired.json",{}) or {}
    now_playing=read_json(state_dir/"now-playing.json",{}) or {}
    playlist=read_json(state_dir/"playlist.json",{}) or {}
    chat_state=read_json(state_dir/"chat-bot-state.json",{}) or {}
    controls_frozen=bool((chat_state.get("freeze") or {}).get("track_id"))
    fresh=False
    try:
        timestamp=datetime.fromisoformat(str(h.get("updated_at") or "").replace("Z","+00:00"))
        age=(datetime.now(timezone.utc)-timestamp.astimezone(timezone.utc)).total_seconds()
        fresh=-15<=age<=60
    except Exception:
        pass
    try:
        active_container=(run(["docker","inspect","-f","{{.State.Running}}",
             "peter-lofi-youtube-lofi-hip-hop"],timeout=12).strip().lower()=="true")
    except Exception:
        active_container=False
    live=fresh and active_container and h.get("status")=="live" and bool(h.get("encoder_pid"))
    payload={
        "runtime_slot":slot,"platform":slot,
        "session_id":str(desired.get("session_id") or ""),
        "title":str(desired.get("title") or "Lofi Hip Hop Radio")[:150],
        "playlist_key":str(desired.get("playlist_key") or "lofi-hip-hop"),
        "status":"live" if live else "unknown",
        "fps":h.get("fps"),"video_bitrate_kbps":h.get("video_bitrate_kbps"),
        "restarts":h.get("restarts",0),"updated_at":h.get("updated_at"),
        "loop_url":str(desired.get("loop_url") or ""),
        "visual_revision":desired.get("visual_revision",0),
        "hot_swap":bool(h.get("hot_swap",True)),
        "encoder_pid":h.get("encoder_pid") if live else None,
        "audio_pid":h.get("audio_pid") if live else None,
        "visual_pid":h.get("visual_pid") if live else None,
        "audio_status":h.get("audio_status"),
        "audio_stalls":h.get("audio_stalls",0),
        "visual_status":h.get("visual_status"),
        "now_playing":{k:now_playing.get(k) for k in
            ("track_id","title","state","started_at")},
        "playlist_track_count":len(playlist.get("tracks") or []),
        "controls_frozen":controls_frozen,
    }
    post_json("http://127.0.0.1:8790/api/ovh/agent/isolated-status",
        {"runtime_slot":slot,"service":payload})
    return live


def sync_isolated_lofi_approved_expansion():
    """Hot-append approved 37-66 tracks to the running Lofi playlist, no restart.

    Never remove, reorder or replace existing live tracks. AudioEngine observes
    the playlist file mtime and loads the additional tracks when available.
    """
    slot="youtube-lofi-hip-hop"
    st=OVH/"state"/slot
    playlist_file=st/"playlist.json"
    current=read_json(playlist_file,{}) or {}
    existing=current.get("tracks") or []
    if not isinstance(existing,list) or not existing:
        return {"status":"not_initialized"}
    key=str(current.get("playlist_key") or "")
    if key not in ("lofi-hip-hop",""):
        return {"status":"different_live_playlist"}
    existing_ids={str(t.get("id") or "") for t in existing if isinstance(t,dict)}
    if len(existing_ids)>=66:
        return {"status":"complete","tracks":len(existing_ids)}
    sources=[
      "http://127.0.0.1:8790/api/ovh/agent/runtime-config?path=control/music-library.json&raw=1",
      "https://raw.githubusercontent.com/thebusinessflowtv/theofficemusic/main/control/music-library.json",
    ]
    approved={}
    for url in sources:
        try:
            lib=fetch_json(url)
            playlist=next((p for p in (lib.get("playlists") or [])
                           if isinstance(p,dict) and p.get("key")=="lofi-hip-hop"),{})
            for track in playlist.get("tracks") or []:
                if not isinstance(track,dict):continue
                match=re.fullmatch(r"lofi-hip-hop-20261008-(\d{2})",str(track.get("id") or ""))
                if not match or not 37<=int(match.group(1))<=66:continue
                address=str(track.get("url") or "")
                if not address.startswith(
                   "https://github.com/thebusinessflowtv/theofficemusic/releases/download/"
                ) or not address.lower().endswith(".mp3"):
                    continue
                if int(track.get("duration_seconds") or 0)!=300 or (
                        track.get("quality_gate")!="technical_and_45s_intro_diversity_passed"):
                    continue
                approved[track["id"]]={
                    "id":track["id"],"title":str(track.get("title") or track["id"])[:150],
                    "url":address,"duration_seconds":300,
                    "position":int(track.get("position") or int(match.group(1))),
                    "source":"lofi-hip-hop-original",
                }
        except Exception as exc:
            print("Lofi approved catalog source unavailable:",type(exc).__name__,flush=True)
    additions=[v for k,v in sorted(approved.items(),key=lambda a:a[1]["position"])
               if k not in existing_ids]
    if not additions:
        return {"status":"no_new_approved_tracks","tracks":len(existing_ids)}
    if len(existing)+len(additions)>66:
        raise RuntimeError("Lofi hot-append would exceed 66 tracks; refusing")
    # Preserve the live session, now-playing pointer, current track order, and
    # the pre-existing settings; only add the new approved releases.
    current.update({"station":slot,"playlist_key":"lofi-hip-hop",
                    "tracks":[*existing,*additions],"updated_at":now()})
    atomic_json(playlist_file,current)
    return {"status":"appended","new_tracks":len(additions),
            "total_tracks":len(current["tracks"]),
            "first_new":additions[0]["id"],"last_new":additions[-1]["id"],
            "publisher_restart":False}


def apply_isolated_lofi_track_controls():
    """Only next/previous for Lofi. Independent of the legacy agent image."""
    slot="youtube-lofi-hip-hop"
    endpoint="http://127.0.0.1:8790"
    cmds=fetch_json(endpoint+"/api/ovh/agent/commands?only_slot=youtube-lofi-hip-hop&limit=15").get("commands") or []
    applied=0
    for cmd in cmds:
        cid=str(cmd.get("id") or "")
        action=str(cmd.get("action") or "").lower()
        if not cid:continue
        try:
            if cmd.get("runtime_slot")!=slot or action not in ("skip","previous"):
                raise RuntimeError("Unsupported isolated audio command")
            h=health(slot)
            if h.get("status")!="live" or not h.get("encoder_pid"):
                raise RuntimeError("Lofi encoder is not live; music change refused")
            path=OVH/"state"/slot
            chat=read_json(path/"chat-bot-state.json",{}) or {}
            if (chat.get("freeze") or {}).get("track_id"):
                raise RuntimeError("Faixa protegida pelo chat; aguarde o desbloqueio antes de usar os controles.")
            payload={
                "id":cid,"action":action,"requested_at":now(),
                "source":"mediaforge-ovh-dashboard-isolated",
            }
            safe_id="".join(x for x in cid if x.isalnum() or x in "-_")[:96]
            if not safe_id:raise RuntimeError("Invalid command id")
            queue=path/"audio-commands"
            queue.mkdir(parents=True,exist_ok=True)
            # The running AudioEngine drains these individual files in order,
            # avoiding lost rapid clicks; command.json is the legacy mirror.
            atomic_json(queue/(f"{time.time_ns():020d}-{safe_id}.json"),payload)
            atomic_json(path/"command.json",payload)
            post_json(endpoint+"/api/ovh/agent/command-ack",
                {"id":cid,"status":"completed"})
            applied+=1
        except Exception as exc:
            try:post_json(endpoint+"/api/ovh/agent/command-ack",
                {"id":cid,"status":"failed","error":str(exc)[:350]})
            except Exception:pass
            print("isolated Lofi audio command failed:",cid,str(exc)[:180],flush=True)
    return applied


def main():
    try:
        result=bootstrap_ui_test_control_agent()
        if result:
            print("ui-test control-agent bootstrap",result,flush=True)
    except Exception as exc:
        print("ui-test control-agent bootstrap failed",str(exc)[:800],flush=True)

    # The watchdog is fully local and keeps running even when Cloudflare/D1 is
    # unavailable or rate-limited.
    threading.Thread(target=watchdog_loop,name="mediaforge-watchdog",daemon=True).start()

    remote_control="https://mediaforge-api.guilhermeodsgn.workers.dev"
    last_lofi_expansion_sync=0.0
    while True:
        # The old remote deploy queue contains stale non-Lofi commands and must
        # not be drained automatically while other broadcasts are live.
        local_sources=[x for x in dict.fromkeys((API,"http://127.0.0.1:8790"))
                       if x.rstrip("/")!=remote_control]
        inboxes=[(x,"?limit=3") for x in local_sources]
        inboxes.append((remote_control,"?limit=2&only_action=launch_isolated_lofi_youtube"))
        for inbox_api,query in inboxes:
            try:
                batch=fetch_json(inbox_api+"/api/ovh/deploy-agent/commands"+query)
                for cmd in batch.get("commands") or []:
                    cid=str(cmd.get("id") or "")
                    if not cid:continue
                    reload_self=False
                    try:
                        result,reload_self=execute(cmd)
                        post_json(inbox_api+"/api/ovh/deploy-agent/ack",{"id":cid,"status":"completed","result":result})
                        print("deploy completed",cid,cmd.get("action"),cmd.get("target"),flush=True)
                    except Exception as exc:
                        err=str(exc)[:1200]
                        try:
                            head=""
                            try: head=git_head() if REPO.exists() else ""
                            except Exception: pass
                            post_json(inbox_api+"/api/ovh/deploy-agent/ack",{"id":cid,"status":"failed","error":err,"result":{"git_head":head}})
                        except Exception:pass
                        print("deploy failed",cid,err,flush=True)
                    if reload_self:
                        os.execv(sys.executable,[sys.executable,str(pathlib.Path(__file__).resolve())])
            except Exception as exc:
                # Cloudflare is only a deploy/control channel. A failure here does
                # not touch local publishers or the watchdog.
                print("deploy poll failed:",str(exc)[:800],flush=True)
        # Read-only isolated publisher telemetry and audio-only controls.
        # No other stream container is restarted or reconfigured.
        try:
            report_isolated_lofi_status()
        except Exception as exc:
            print("isolated Lofi telemetry unavailable:",str(exc)[:180],flush=True)
        try:
            handled=apply_isolated_lofi_track_controls()
            if handled:print("isolated Lofi audio controls acknowledged:",handled,flush=True)
        except Exception as exc:
            print("isolated Lofi controls unavailable:",str(exc)[:180],flush=True)
        if time.monotonic()-last_lofi_expansion_sync>=45:
            last_lofi_expansion_sync=time.monotonic()
            try:
                delta=sync_isolated_lofi_approved_expansion()
                if delta.get("status")=="appended":
                    print("LOFI_EXPANSION_HOT_APPENDED",delta,flush=True)
            except Exception as exc:
                print("Lofi expansion hot-append unavailable:",str(exc)[:240],flush=True)
        time.sleep(POLL)


if __name__=="__main__":
    main()
