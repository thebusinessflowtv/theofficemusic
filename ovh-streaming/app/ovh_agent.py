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
import subprocess
import signal
import urllib.request
import uuid
import zipfile
from datetime import datetime, timezone

STATE=pathlib.Path("/state")
API=os.environ.get("MEDIAFORGE_API_URL","http://127.0.0.1:8790").rstrip("/")
REMOTE_CONTROL_API=os.environ.get("MEDIAFORGE_REMOTE_CONTROL_API","https://mediaforge-api.guilhermeodsgn.workers.dev").rstrip("/")
AGENT_TOKEN=os.environ.get("MEDIAFORGE_AGENT_TOKEN","").strip()
POLL=max(3,int(os.environ.get("OVH_AGENT_POLL_SECONDS","5")))
LOCAL_STATUS_SECONDS=max(5,int(os.environ.get("OVH_LOCAL_STATUS_SECONDS","10")))
REMOTE_STATUS_SECONDS=max(5,int(os.environ.get("OVH_REMOTE_STATUS_SECONDS","10")))
GITHUB_RAW_BASE=os.environ.get("MEDIAFORGE_GITHUB_RAW_BASE","https://raw.githubusercontent.com/thebusinessflowtv/theofficemusic/main").rstrip("/")
GITHUB_FALLBACK_MAX_AGE_SECONDS=max(30,int(os.environ.get("OVH_GITHUB_FALLBACK_MAX_AGE_SECONDS","900")))
SLOTS=("kick","twitch","youtube-deep-house","youtube-rainy","youtube-ui-test")
AGENT_DIR=STATE/"agent"
PROCESSED=AGENT_DIR/"processed.json"
LOCAL_STATUS=AGENT_DIR/"status.json"
AGENT_DIR.mkdir(parents=True,exist_ok=True)
UI_TEST_PROC=None
UI_TEST_LOG=None

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


def github_fetch_json(path):
    url=GITHUB_RAW_BASE+"/"+str(path).lstrip("/")+"?ts="+str(int(time.time()*1000))
    req=urllib.request.Request(url,headers={"User-Agent":"MediaForge-OVH-Agent-GitHub-Fallback","Cache-Control":"no-cache"})
    with urllib.request.urlopen(req,timeout=20) as r:
        return json.loads(r.read().decode("utf-8"))


def command_is_recent(cmd,entry=None):
    raw=str((cmd or {}).get("requested_at") or (entry or {}).get("created_at") or "")
    if not raw:
        return False
    try:
        dt=datetime.fromisoformat(raw.replace("Z","+00:00"))
        age=(datetime.now(timezone.utc)-dt.astimezone(timezone.utc)).total_seconds()
        return -60 <= age <= GITHUB_FALLBACK_MAX_AGE_SECONDS
    except Exception:
        return False


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
    manifest=cmd.get("manifest") if isinstance(cmd.get("manifest"),dict) else None
    manifest_url=str(cmd.get("manifest_url") or "").strip()
    if not archive_url:
        raise ValueError("archive_url is required")
    if manifest is None:
        if not manifest_url:
            raise ValueError("manifest or manifest_url is required")
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


def _ui_test_secret_path():
    return STATE/"youtube-ui-test"/"runtime-secret.json"


def ensure_ui_test_process():
    global UI_TEST_PROC, UI_TEST_LOG
    st=STATE/"youtube-ui-test"
    desired=read_json(st/"desired.json",{}) or {}
    wants_live=str(desired.get("desired") or "stopped").lower() not in {"stopped","stop","offline"}
    if not wants_live:
        if UI_TEST_PROC and UI_TEST_PROC.poll() is None:
            try:
                UI_TEST_PROC.send_signal(signal.SIGTERM)
                UI_TEST_PROC.wait(timeout=8)
            except Exception:
                try: UI_TEST_PROC.kill()
                except Exception: pass
        UI_TEST_PROC=None
        if UI_TEST_LOG:
            try: UI_TEST_LOG.close()
            except Exception: pass
            UI_TEST_LOG=None
        return

    if UI_TEST_PROC and UI_TEST_PROC.poll() is None:
        return

    secret=read_json(_ui_test_secret_path(),{}) or {}
    stream_url=str(secret.get("stream_url") or "").strip()
    stream_key=str(secret.get("stream_key") or "").strip()
    if not stream_url or not stream_key:
        return

    playlist=st/"playlist.json"
    if not playlist.exists():
        src=pathlib.Path("/config/youtube-deep-house.json")
        if src.exists():
            shutil.copyfile(src,playlist)

    env=os.environ.copy()
    env.update({
        "STREAM_URL":stream_url,
        "STREAM_KEY":stream_key,
        "LOOP_URL":str(desired.get("loop_url") or ""),
        "PLAYLIST_FILE":"/config/youtube-deep-house.json",
        "VIDEO_FPS":"30",
        "VIDEO_BITRATE_KBPS":"4500",
        "VIDEO_BUFSIZE_KBPS":"9000",
        "AUDIO_BITRATE_KBPS":"160",
        "VIDEO_PROFILE":"main",
        "VIDEO_PRESET":"superfast",
        "STREAM_PROFILE_VERSION":"ui-test-v1",
        "STARTUP_PREROLL_SECONDS":"1.5",
        "AUDIO_READY_TIMEOUT_SECONDS":"90",
        "VIDEO_UDP_PORT":"19140",
        "BOOTSTRAP_SESSION_ID":str(desired.get("session_id") or "ui-test"),
        "BOOTSTRAP_TITLE":str(desired.get("title") or "Peter Lofi UI Test"),
    })
    UI_TEST_LOG=open(st/"controller.log","ab",buffering=0)
    UI_TEST_PROC=subprocess.Popen(
        ["python","/app/stream_core.py","--platform","youtube-ui-test"],
        env=env,stdout=UI_TEST_LOG,stderr=UI_TEST_LOG,
    )


def apply_command(cmd):
    slot=slot_for(cmd)
    if not slot:
        return

    st=STATE/slot
    st.mkdir(parents=True,exist_ok=True)
    desired_path=st/"desired.json"
    desired=read_json(desired_path,{}) or {}
    action=str(cmd.get("action") or "start").lower()

    if slot=="youtube-ui-test":
        if action in {"start","resume","restart"}:
            stream_url=str(cmd.get("stream_url") or "").strip()
            stream_key=str(cmd.get("stream_key") or "").strip()
            secret_path=_ui_test_secret_path()
            if stream_url and stream_key:
                atomic_json(secret_path,{"stream_url":stream_url,"stream_key":stream_key,"updated_at":iso_now()})
                try: os.chmod(secret_path,0o600)
                except Exception: pass
            elif not secret_path.exists():
                raise ValueError("stream_url and stream_key are required for first youtube-ui-test start")

            assets_dir=STATE/"ui-test-assets"
            assets_dir.mkdir(parents=True,exist_ok=True)
            header_url=str(cmd.get("header_url") or "").strip()
            web_url=str(cmd.get("web_url") or "").strip()
            icon_url=str(cmd.get("icon_url") or "").strip()
            font_carrier_url=str(cmd.get("font_carrier_url") or "").strip()
            header_path=assets_dir/"latest-subscriptions.png"
            web_path=assets_dir/"spider-web.png"
            icon_path=assets_dir/"subscriber-icon.png"
            font_path=assets_dir/"superstar.ttf"
            if header_url and not header_path.exists():
                download_file(header_url,header_path)
            if web_url and not web_path.exists():
                download_file(web_url,web_path)
            if icon_url and not icon_path.exists():
                download_file(icon_url,icon_path)
            if font_carrier_url and not font_path.exists():
                carrier=assets_dir/"font-carrier.png"
                download_file(font_carrier_url,carrier)
                raw=subprocess.check_output([
                    "ffmpeg","-v","error","-i",str(carrier),
                    "-f","rawvideo","-pix_fmt","rgb24","pipe:1"
                ])
                if len(raw)<8:
                    raise RuntimeError("font carrier decode failed")
                size=int.from_bytes(raw[:4],"big")
                data=raw[4:4+size]
                if len(data)!=size or size<1000:
                    raise RuntimeError("font carrier payload invalid")
                font_path.write_bytes(data)
                carrier.unlink(missing_ok=True)
            if not header_path.exists() or not web_path.exists() or not icon_path.exists() or not font_path.exists():
                raise RuntimeError("ui-test Figma/font assets are missing")

            if not (st/"playlist.json").exists():
                src=pathlib.Path("/config/youtube-deep-house.json")
                if src.exists():
                    shutil.copyfile(src,st/"playlist.json")

            desired.update({
                "runtime":"ovh",
                "runtime_slot":"youtube-ui-test",
                "session_id":str(cmd.get("session_id") or desired.get("session_id") or "ui-test"),
                "title":str(cmd.get("title") or desired.get("title") or "Peter Lofi UI Test"),
                "loop_url":str(cmd.get("loop_url") or desired.get("loop_url") or ""),
                "playlist_key":"deep-house-radio-test",
                "desired":"live",
                "generation":next_generation(desired),
                "visual_revision":next_visual_revision(desired),
                "updated_at":iso_now(),
            })
            atomic_json(desired_path,desired)
            ensure_ui_test_process()
            return
        if action=="stop":
            desired.update({
                "desired":"stopped",
                "generation":next_generation(desired),
                "updated_at":iso_now(),
            })
            atomic_json(desired_path,desired)
            ensure_ui_test_process()
            return

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
        # Realtime audio controls are queued as unique files so rapid clicks can
        # never overwrite each other. command.json is kept only as a legacy
        # mirror for older AudioEngine builds; the new engine de-duplicates IDs.
        command_id=str(cmd.get("id") or uuid.uuid4())
        payload={
            "id":command_id,
            "action":action,
            "requested_at":iso_now(),
            "source":str(cmd.get("source") or "mediaforge"),
        }
        qdir=st/"audio-commands"
        qdir.mkdir(parents=True,exist_ok=True)
        safe_id="".join(ch for ch in command_id if ch.isalnum() or ch in "-_")[:96] or uuid.uuid4().hex
        qname=f"{time.time_ns():020d}-{safe_id}.json"
        atomic_json(qdir/qname,payload)
        atomic_json(st/"command.json",payload)
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
        "encoder_pid":h.get("encoder_pid"),
        "audio_pid":h.get("audio_pid"),
        "visual_pid":h.get("visual_pid"),
        "audio_status":ah.get("state") or ah.get("status"),
        "queued_actions":int(ah.get("queued_actions") or 0),
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


def process_one_command(cmd,processed,transport,ack_api=None):
    cid=str((cmd or {}).get("id") or "")
    ack_base=(ack_api or API).rstrip("/")
    if not cid:
        return None
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
            "transport":transport,
        }
        try:
            post_json(ack_base+"/api/ovh/agent/command-ack",{"id":cid,"status":"completed"})
        except Exception as ack_exc:
            print("command ack failed:",cid,ack_exc,flush=True)
        print("processed",last_cmd,flush=True)
        return last_cmd
    except Exception as exc:
        try:
            post_json(ack_base+"/api/ovh/agent/command-ack",{
                "id":cid,
                "status":"failed",
                "error":str(exc)[:500],
            })
        except Exception:
            pass
        print("command apply failed",cid,exc,flush=True)
        return None


def poll_github_fallback(processed):
    handled=[]
    try:
        idx=github_fetch_json("control/ovh-commands/index.json") or {}
        entries=list(idx.get("commands") or [])[-50:]
        for entry in entries:
            cid=str((entry or {}).get("id") or "")
            path=str((entry or {}).get("path") or "")
            if not cid or not path or cid in processed:
                continue
            try:
                cmd=github_fetch_json(path)
            except Exception as exc:
                print("github fallback command fetch failed:",cid,exc,flush=True)
                continue
            if not isinstance(cmd,dict) or str(cmd.get("id") or "")!=cid:
                continue
            if not command_is_recent(cmd,entry):
                continue
            last=process_one_command(cmd,processed,"github-fallback")
            if last:
                handled.append(last)
    except Exception as exc:
        print("github fallback poll failed:",exc,flush=True)
    return handled


def main():
    processed=set(read_json(PROCESSED,[]) or [])
    last_local=0.0
    last_remote=0.0
    last_cmd=None

    while True:
        try:
            ensure_ui_test_process()
        except Exception as exc:
            print("ui test supervisor failed:",exc,flush=True)
        cloud_ok=False
        # Cloudflare is the primary inbox, but GitHub is an independent fallback
        # because every MediaForge command is already mirrored there.
        try:
            batch=fetch_json(API+"/api/ovh/agent/commands?limit=20")
            cloud_ok=True
            for cmd in batch.get("commands") or []:
                last=process_one_command(cmd,processed,"cloudflare-control")
                if last:
                    last_cmd=last
        except Exception as exc:
            print("cloud control poll failed:",exc,flush=True)

        # The public GitHub-Pages MediaForge currently posts realtime controls
        # to the remote API. Consume that queue as a secondary inbox while the
        # local OVH API remains authoritative for runtime/status. This does not
        # make live playback depend on Cloudflare: if it is unavailable, the
        # local inbox and live continue normally.
        if REMOTE_CONTROL_API and REMOTE_CONTROL_API != API:
            try:
                remote=fetch_json(REMOTE_CONTROL_API+"/api/ovh/agent/commands?limit=20")
                for cmd in remote.get("commands") or []:
                    last=process_one_command(cmd,processed,"remote-ui-control",ack_api=REMOTE_CONTROL_API)
                    if last:
                        last_cmd=last
            except Exception as exc:
                print("remote UI control poll failed:",exc,flush=True)

        # GitHub remains a fallback only for non-realtime legacy commands.
        fallback_handled=poll_github_fallback(processed)
        if fallback_handled:
            last_cmd=fallback_handled[-1]

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
