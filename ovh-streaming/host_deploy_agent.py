#!/usr/bin/env python3
"""MediaForge host deploy agent.

Runs on the OVH host via systemd. It intentionally exposes NO shell surface:
only a fixed allow-list of deploy/health/rollback operations is accepted.
"""
import json
import os
import pathlib
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timezone

API=os.environ.get("MEDIAFORGE_API_URL","https://mediaforge-api.guilhermeodsgn.workers.dev").rstrip("/")
REPO=pathlib.Path(os.environ.get("MEDIAFORGE_REPO","/home/ubuntu/theofficemusic"))
OVH=REPO/"ovh-streaming"
STATE_DIR=pathlib.Path("/var/lib/mediaforge-deploy-agent")
STATE_FILE=STATE_DIR/"state.json"
DOCKER_CONFIG_DIR=STATE_DIR/"docker"
POLL=max(3,int(os.environ.get("MEDIAFORGE_DEPLOY_POLL_SECONDS","5")))
SLOTS=("kick","twitch","youtube-deep-house","youtube-rainy")
SERVICES=("ovh-agent","control-api")+SLOTS
CONTAINERS={
    "ovh-agent":"peter-lofi-ovh-agent",
    "control-api":"peter-lofi-control-api",
    "kick":"peter-lofi-kick",
    "twitch":"peter-lofi-twitch",
    "youtube-deep-house":"peter-lofi-youtube-deep-house",
    "youtube-rainy":"peter-lofi-youtube-rainy",
}

STATE_DIR.mkdir(parents=True,exist_ok=True)
DOCKER_CONFIG_DIR.mkdir(parents=True,exist_ok=True)

def now():
    return datetime.now(timezone.utc).isoformat().replace("+00:00","Z")

def fetch_json(url):
    req=urllib.request.Request(url+("&" if "?" in url else "?")+"ts="+str(int(time.time()*1000)),headers={"User-Agent":"MediaForge-Host-Deploy-Agent"})
    with urllib.request.urlopen(req,timeout=30) as r:
        return json.loads(r.read().decode("utf-8"))

def post_json(url,payload):
    data=json.dumps(payload).encode("utf-8")
    req=urllib.request.Request(url,data=data,method="POST",headers={"content-type":"application/json","user-agent":"MediaForge-Host-Deploy-Agent"})
    with urllib.request.urlopen(req,timeout=30) as r:
        return json.loads(r.read().decode("utf-8") or "{}")

def run(args,cwd=None,timeout=1200):
    env=os.environ.copy()
    # systemd hardening can make /root read-only. Keep all Docker/Buildx
    # state in the agent's writable state directory instead.
    env.setdefault("DOCKER_CONFIG",str(DOCKER_CONFIG_DIR))
    p=subprocess.run(args,cwd=str(cwd) if cwd else None,text=True,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,timeout=timeout,check=False,env=env)
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

def git_head():
    return run(["runuser","-u","ubuntu","--","git","-C",str(REPO),"rev-parse","HEAD"],timeout=30).strip().splitlines()[-1]

def git_sync():
    old=git_head()
    run(["runuser","-u","ubuntu","--","git","-C",str(REPO),"fetch","origin","main"],timeout=180)
    run(["runuser","-u","ubuntu","--","git","-C",str(REPO),"reset","--hard","origin/main"],timeout=60)
    new=git_head()
    return old,new

def health(slot):
    if slot in SLOTS:
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

def deploy_service(service):
    if service not in SERVICES:
        raise ValueError("service not allowed")
    before=health(service)
    backup=backup_image(service)
    run(["docker","compose","build",service],cwd=OVH,timeout=1800)
    run(["docker","compose","up","-d","--no-deps","--force-recreate",service],cwd=OVH,timeout=300)
    h=wait_healthy(
        service,
        240,
        after_updated_at=before.get("updated_at"),
        require_hot_swap=service in SLOTS,
    )
    return {"service":service,"health":h,"backup_image":backup}

def deploy_all():
    results=[]
    # Build first, then replace one service at a time.
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
        run(["docker","cp",str(OVH/"app"/"audio_engine.py"),f"{name}:/app/audio_engine.py"],timeout=30)
        run(["docker","cp",str(OVH/"app"/"stream_core.py"),f"{name}:/app/stream_core.py"],timeout=30)

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
    return {"git_head":git_head(),"services":{s:health(s) for s in SERVICES}}

def execute(cmd):
    action=str(cmd.get("action") or "")
    target=str(cmd.get("target") or "")
    reload_self=False
    if action=="health_check":
        return health_all(),False
    if action=="rollback_service":
        return rollback_service(target),False

    old,new=git_sync()
    if action=="hot_patch_streaming":
        return {"old_head":old,"new_head":new,"targets":hot_patch_streaming(target)},False
    st=read_state()
    if old!=new:
        st.update({"previous_head":old,"last_good_head":new,"last_deploy_at":now()})
        write_state(st)

    if action=="deploy_service":
        return {"old_head":old,"new_head":new,**deploy_service(target)},False
    if action=="deploy_all":
        return {"old_head":old,"new_head":new,"services":deploy_all()},False
    if action=="deploy_host_agent":
        # Source was updated by git_sync. ACK first, then exec the fresh code.
        return {"old_head":old,"new_head":new,"host_agent":"reloading"},True
    raise ValueError("action not allowed")

def main():
    while True:
        try:
            batch=fetch_json(API+"/api/ovh/deploy-agent/commands?limit=3")
            for cmd in batch.get("commands") or []:
                cid=str(cmd.get("id") or "")
                if not cid:continue
                reload_self=False
                try:
                    result,reload_self=execute(cmd)
                    post_json(API+"/api/ovh/deploy-agent/ack",{"id":cid,"status":"completed","result":result})
                    print("deploy completed",cid,cmd.get("action"),cmd.get("target"),flush=True)
                except Exception as exc:
                    err=str(exc)[:1200]
                    try:
                        head=""
                        try: head=git_head() if REPO.exists() else ""
                        except Exception: pass
                        post_json(API+"/api/ovh/deploy-agent/ack",{"id":cid,"status":"failed","error":err,"result":{"git_head":head}})
                    except Exception:pass
                    print("deploy failed",cid,err,flush=True)
                if reload_self:
                    os.execv(sys.executable,[sys.executable,str(pathlib.Path(__file__).resolve())])
        except Exception as exc:
            print("deploy poll failed:",str(exc)[:800],flush=True)
        time.sleep(POLL)

if __name__=="__main__":
    main()
