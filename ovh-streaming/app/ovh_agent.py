#!/usr/bin/env python3
"""MediaForge OVH live-control agent.

Live state belongs to OVH. Cloudflare is only a control/UI transport:
- local desired/playlist/now-playing/health state is authoritative;
- local heartbeat remains frequent;
- Cloudflare status sync is throttled;
- GitHub is not polled at runtime;
- visual/playlist/skip/previous changes never restart the RTMP session.
"""
import hashlib
import json
import os
import pathlib
import shutil
import time
import urllib.request
import uuid
import zipfile
from datetime import datetime, timezone

STATE=pathlib.Path("/state")
API=os.environ.get("MEDIAFORGE_API_URL","http://host.docker.internal:8790").rstrip("/")
AGENT_TOKEN=os.environ.get("MEDIAFORGE_AGENT_TOKEN","").strip()
POLL=max(3,int(os.environ.get("OVH_AGENT_POLL_SECONDS","5")))
LOCAL_STATUS_SECONDS=max(5,int(os.environ.get("OVH_LOCAL_STATUS_SECONDS","10")))
REMOTE_STATUS_SECONDS=max(30,int(os.environ.get("OVH_REMOTE_STATUS_SECONDS","60")))
SLOTS=("kick","twitch","youtube-deep-house","youtube-rainy")
AGENT_DIR=STATE/"agent"
PROCESSED=AGENT_DIR/"processed.json"
LOCAL_STATUS=AGENT_DIR/"status.json"
AGENT_DIR.mkdir(parents=True,exist_ok=True)

# These sources represent ordinary hot changes and are never allowed to
# interrupt a live RTMP session, even if an upstream bug labels them restart.
NON_INTERRUPT_SOURCES=(
    "mediaforge-visual-switch",
    "mediaforge-playlist-switch",
    "library-sync",
)


def iso_now():
    return datetime.now(timezone.utc).isoformat().replace("+00:00","Z")


def read_json(path,default=None):
    try:return json.loads(path.read_text(encoding="utf-8"))
    except Exception:return default


def atomic_json(path,payload):
    path.parent.mkdir(parents=True,exist_ok=True)
    tmp=path.with_suffix(path.suffix+".tmp")
    tmp.write_text(json.dumps(payload,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    tmp.replace(path)


def agent_headers(extra=None):
    headers={"User-Agent":"MediaForge-OVH-Agent"}
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
    with urllib.request.urlopen(req,timeout=20) as r:
        return json.loads(r.read().decode("utf-8"))


def post_json(url,payload):
    data=json.dumps(payload).encode("utf-8")
    req=urllib.request.Request(
        url,
        data=data,
        method="POST",
        headers=agent_headers({"content-type":"application/json"}),
    )
    with urllib.request.urlopen(req,timeout=20) as r:
        return r.read()


def slot_for(cmd):
    slot=str(cmd.get("runtime_slot") or cmd.get("slot") or cmd.get("platform") or "")
    if slot=="youtube":
        slot=str(cmd.get("youtube_slot") or "")
    return slot if slot in SLOTS else ""


def next_generation(desired):
    try:return int(desired.get("generation") or 0)+1
    except Exception:return int(time.time())


def next_visual_revision(desired):
    try:return int(desired.get("visual_revision") or 0)+1
    except Exception:return int(time.time())


def source_is_non_interrupting(cmd):
    source=str(cmd.get("source") or "").strip().lower()
    return any(source.startswith(prefix) for prefix in NON_INTERRUPT_SOURCES)


def download_file(url,target):
    target.parent.mkdir(parents=True,exist_ok=True)
    temp=target.with_suffix(target.suffix+".part")
    temp.unlink(missing_ok=True)
    req=urllib.request.Request(str(url),headers={"User-Agent":"MediaForge-Twitch-DJ-Importer"})
    with urllib.request.urlopen(req,timeout=60) as r, open(temp,"wb") as fh:
        while True:
            chunk=r.read(1024*1024)
            if not chunk:
                break
            fh.write(chunk)
    if not temp.exists() or temp.stat().st_size<1024:
        raise RuntimeError("DJ archive download is empty")
    temp.replace(target)
    return target


def import_twitch_dj_archive(cmd):
    if slot_for(cmd)!="twitch":
        raise ValueError("DJ archive import is Twitch-only")
    archive_url=str(cmd.get("archive_url") or "").strip()
    manifest_url=str(cmd.get("manifest_url") or "").strip()
    if not archive_url or not manifest_url:
        raise ValueError("archive_url and manifest_url are required")

    manifest=fetch_json(manifest_url)
    expected={}
    for row in manifest.get("tracks") or []:
        h=str(row.get("sha256") or "").lower().strip()
        if len(h)==64:
            expected[h]=row
    if not expected:
        raise RuntimeError("DJ manifest has no hashes")

    dj_dir=STATE/"twitch-dj-audio"
    dj_dir.mkdir(parents=True,exist_ok=True)
    archive=STATE/"twitch-dj-import.zip"
    status_path=STATE/"twitch"/"dj-import.json"
    atomic_json(status_path,{"status":"downloading","updated_at":iso_now(),"expected":len(expected)})
    download_file(archive_url,archive)

    found={}
    rejected=[]
    duplicates=0
    atomic_json(status_path,{"status":"validating","updated_at":iso_now(),"expected":len(expected)})
    with zipfile.ZipFile(archive,"r") as zf:
        for info in zf.infolist():
            if info.is_dir() or not str(info.filename).lower().endswith(".mp3"):
                continue
            h=hashlib.sha256()
            with zf.open(info,"r") as src:
                while True:
                    chunk=src.read(1024*1024)
                    if not chunk:
                        break
                    h.update(chunk)
            digest=h.hexdigest()
            meta=expected.get(digest)
            if not meta:
                rejected.append({"filename":pathlib.PurePosixPath(info.filename).name,"sha256":digest})
                continue
            if digest in found:
                duplicates+=1
                continue
            target=dj_dir/(digest+".mp3")
            temp=target.with_suffix(".mp3.part")
            with zf.open(info,"r") as src, open(temp,"wb") as dst:
                shutil.copyfileobj(src,dst,1024*1024)
            temp.replace(target)
            found[digest]={
                "id":"twitch-dj-"+digest[:12],
                "title":str(meta.get("title") or pathlib.PurePosixPath(info.filename).stem),
                "artists":str(meta.get("artists") or ""),
                "url":"file://"+str(target),
                "duration_seconds":float(meta.get("duration_seconds") or 0),
                "source":"twitch_dj_catalog_licensed_copy",
                "sha256":digest,
            }

    missing=sorted(set(expected)-set(found))
    if not found:
        raise RuntimeError("No validated Twitch DJ MP3 found in archive")

    base=[x for x in (cmd.get("base_tracks") or []) if isinstance(x,dict) and x.get("url")]
    mixed=[]
    for i,t in enumerate(base):
        mixed.append({
            "id":str(t.get("id") or f"twitch-dj-original-{i+1:02d}"),
            "title":str(t.get("title") or "Peter Lofi"),
            "url":str(t["url"]),
            "duration_seconds":float(t.get("duration_seconds") or 0),
            "source":"peter_lofi_original",
        })
    mixed.extend(found.values())

    st=STATE/"twitch"
    atomic_json(st/"playlist.json",{
        "station":"twitch",
        "playlist_key":"twitch-dj-mixed",
        "platform_lock":["twitch"],
        "shuffle":True,
        "repeat":True,
        "updated_at":iso_now(),
        "tracks":mixed,
    })
    desired=read_json(st/"desired.json",{}) or {}
    desired.update({
        "runtime":"ovh",
        "runtime_slot":"twitch",
        "playlist_key":"twitch-dj-mixed",
        "updated_at":iso_now(),
    })
    atomic_json(st/"desired.json",desired)
    # Only interrupt the current audio track with a fade; generation remains untouched,
    # so the persistent Twitch RTMP encoder stays connected.
    atomic_json(st/"command.json",{
        "id":str(cmd.get("id") or uuid.uuid4()),
        "action":"skip",
        "requested_at":iso_now(),
        "source":"mediaforge-twitch-dj-import",
    })
    result={
        "status":"ready",
        "updated_at":iso_now(),
        "playlist_key":"twitch-dj-mixed",
        "original_tracks":len(base),
        "commercial_tracks":len(found),
        "track_count":len(mixed),
        "duplicates_ignored":duplicates,
        "missing_hashes":missing,
        "rejected_files":rejected,
        "rtmp_restart":False,
    }
    atomic_json(status_path,result)
    try:
        archive.unlink(missing_ok=True)
    except Exception:
        pass
    return result


def apply_command(cmd):
    slot=slot_for(cmd)
    if not slot:
        return

    st=STATE/slot
    st.mkdir(parents=True,exist_ok=True)
    desired_path=st/"desired.json"
    desired=read_json(desired_path,{}) or {}
    action=str(cmd.get("action") or "start").lower()

    if action=="import_twitch_dj_archive":
        import_twitch_dj_archive(cmd)
        return

    # Playlist payloads are persisted locally on OVH. AudioEngine keeps a
    # persistent local cache under /state/audio-cache.
    tracks=cmd.get("tracks")
    if isinstance(tracks,list) and tracks:
        atomic_json(st/"playlist.json",{
            "station":slot,
            "playlist_key":str(cmd.get("playlist_key") or ""),
            "shuffle":bool(cmd.get("shuffle",True)),
            "repeat":bool(cmd.get("repeat",True)),
            "updated_at":iso_now(),
            "tracks":tracks,
        })

    if action in {"skip","previous"}:
        atomic_json(st/"command.json",{
            "id":str(cmd.get("id") or uuid.uuid4()),
            "action":action,
            "requested_at":iso_now(),
            "source":str(cmd.get("source") or "mediaforge"),
        })
        return

    if action in {"update_playlist","set_playlist"}:
        desired=read_json(desired_path,{}) or {}
        desired.update({
            "runtime":"ovh",
            "runtime_slot":slot,
            "session_id":str(cmd.get("session_id") or desired.get("session_id") or ""),
            "title":str(cmd.get("title") or desired.get("title") or ""),
            "playlist_key":str(cmd.get("playlist_key") or desired.get("playlist_key") or ""),
            "updated_at":iso_now(),
        })
        atomic_json(desired_path,desired)
        if action=="set_playlist":
            atomic_json(st/"command.json",{
                "id":str(cmd.get("id") or uuid.uuid4()),
                "action":"skip",
                "requested_at":iso_now(),
                "source":"mediaforge-playlist-switch",
            })
        return

    if action=="set_visual":
        loop_url=str(cmd.get("loop_url") or "").strip()
        if not loop_url:
            raise ValueError("loop_url is required for set_visual")
        desired=read_json(desired_path,{}) or {}
        desired.update({
            "runtime":"ovh",
            "runtime_slot":slot,
            "session_id":str(cmd.get("session_id") or desired.get("session_id") or ""),
            "title":str(cmd.get("title") or desired.get("title") or ""),
            "loop_url":loop_url,
            "desired":"live",
            # Hot visual change: generation is intentionally untouched.
            "visual_revision":next_visual_revision(desired),
            "updated_at":iso_now(),
        })
        atomic_json(desired_path,desired)
        return

    desired.update({
        "runtime":"ovh",
        "runtime_slot":slot,
        "session_id":str(cmd.get("session_id") or desired.get("session_id") or ""),
        "title":str(cmd.get("title") or desired.get("title") or ""),
        "loop_url":str(cmd.get("loop_url") or desired.get("loop_url") or ""),
        "updated_at":iso_now(),
    })

    # Safety rail: ordinary visual/library operations are forbidden from
    # becoming a restart/stop, even if the UI accidentally labels them so.
    if action in {"restart","stop"} and source_is_non_interrupting(cmd):
        raise ValueError(f"{action} blocked for non-interrupting source {cmd.get('source')}")

    if action=="stop":
        desired["desired"]="stopped"
        desired["generation"]=next_generation(desired)
    elif action=="restart":
        desired["desired"]="live"
        desired["generation"]=next_generation(desired)
    elif action in {"start","resume"}:
        # Idempotent when already live: do not bump generation and therefore do
        # not tear down the current RTMP connection.
        was_live=str(desired.get("desired") or "live").lower() not in {"stopped","stop","offline"}
        desired["desired"]="live"
        if not was_live:
            desired["generation"]=next_generation(desired)

    atomic_json(desired_path,desired)


def host_metrics():
    out={}
    try:
        out["load_1m"]=float(pathlib.Path("/proc/loadavg").read_text().split()[0])
    except Exception:pass
    try:
        vals={}
        for line in pathlib.Path("/proc/meminfo").read_text().splitlines():
            if ":" in line:
                k,v=line.split(":",1)
                vals[k]=int(v.strip().split()[0])
        total=vals.get("MemTotal",0)
        avail=vals.get("MemAvailable",0)
        if total:
            out["memory_percent"]=round((total-avail)*100/total,1)
    except Exception:pass
    try:
        du=shutil.disk_usage("/state")
        out["disk_percent"]=round((du.total-du.free)*100/du.total,1)
    except Exception:pass
    try:
        out["uptime_seconds"]=float(pathlib.Path("/proc/uptime").read_text().split()[0])
    except Exception:pass
    return out


def service_payload(slot):
    st=STATE/slot
    h=read_json(st/"health.json",{}) or {}
    n=read_json(st/"now-playing.json",{}) or {}
    d=read_json(st/"desired.json",{}) or {}
    ah=read_json(st/"audio-health.json",{}) or {}
    vh=read_json(st/"visual-health.json",{}) or {}
    dj=read_json(st/"dj-import.json",{}) or {}
    playlist=read_json(st/"playlist.json",{}) or {}
    return {
        "runtime_slot":slot,
        "platform":h.get("platform") or slot,
        "session_id":h.get("session_id") or d.get("session_id") or "",
        "title":h.get("title") or d.get("title") or "",
        "playlist_key":d.get("playlist_key") or "",
        "status":h.get("status") or ("live" if d.get("desired")=="live" else "unknown"),
        "fps":h.get("fps"),
        "video_bitrate_kbps":h.get("video_bitrate_kbps"),
        "restarts":h.get("restarts",0),
        "updated_at":h.get("updated_at"),
        "loop_url":h.get("loop_url") or d.get("loop_url") or "",
        "visual_revision":d.get("visual_revision") or 0,
        "hot_swap":bool(h.get("hot_swap",False)),
        "audio_status":ah.get("state") or ah.get("status"),
        "audio_stalls":ah.get("stalls",0),
        "visual_status":vh.get("status"),
        "now_playing":n,
        "playlist_track_count":len(playlist.get("tracks") or []),
        "dj_import":dj,
    }


def status_payload(processed_count=0,last_command=None):
    payload={
        "agent_id":"ovh-main",
        "runtime":"ovh",
        "authority":"local",
        "cloudflare_required_for_live":False,
        "github_runtime_polling":False,
        "reported_at":iso_now(),
        "host":host_metrics(),
        "services":{s:service_payload(s) for s in SLOTS},
        "processed_commands":processed_count,
    }
    if last_command:
        payload["last_command"]=last_command
    return payload


def write_local_status(processed_count=0,last_command=None):
    payload=status_payload(processed_count,last_command)
    atomic_json(LOCAL_STATUS,payload)
    return payload


def report_remote(payload):
    # Cloudflare is a UI/control-plane mirror only. If unavailable or capped,
    # local streams and local status continue untouched.
    try:
        post_json(API+"/api/ovh/agent/status",payload)
        return True
    except Exception as exc:
        print("remote status sync failed:",exc,flush=True)
        return False


def main():
    processed=set(read_json(PROCESSED,[]) or [])
    last_local=0.0
    last_remote=0.0
    last_cmd=None

    while True:
        # Cloudflare is only an optional command inbox. A D1 outage cannot stop
        # or restart any local publisher.
        try:
            batch=fetch_json(API+"/api/ovh/agent/commands?limit=20")
            for cmd in batch.get("commands") or []:
                cid=str(cmd.get("id") or "")
                if not cid:
                    continue
                try:
                    if cid not in processed:
                        apply_command(cmd)
                        processed.add(cid)
                        atomic_json(PROCESSED,sorted(processed)[-500:])
                    last_cmd={
                        "id":cid,
                        "action":cmd.get("action"),
                        "runtime_slot":slot_for(cmd),
                        "processed_at":iso_now(),
                        "transport":"cloudflare-control",
                    }
                    post_json(API+"/api/ovh/agent/command-ack",{"id":cid,"status":"completed"})
                    print("processed",last_cmd,flush=True)
                except Exception as exc:
                    try:
                        post_json(API+"/api/ovh/agent/command-ack",{
                            "id":cid,
                            "status":"failed",
                            "error":str(exc)[:500],
                        })
                    except Exception:
                        pass
                    print("command apply failed",cid,exc,flush=True)
        except Exception as exc:
            print("cloud control poll failed:",exc,flush=True)

        ts=time.time()
        payload=None
        if ts-last_local>=LOCAL_STATUS_SECONDS:
            payload=write_local_status(len(processed),last_cmd)
            last_local=ts
        if ts-last_remote>=REMOTE_STATUS_SECONDS:
            if payload is None:
                payload=write_local_status(len(processed),last_cmd)
                last_local=ts
            report_remote(payload)
            last_remote=ts

        time.sleep(POLL)


if __name__=="__main__":
    main()
