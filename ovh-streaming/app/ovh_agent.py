#!/usr/bin/env python3
import json, os, pathlib, shutil, time, urllib.request, uuid
from datetime import datetime, timezone

STATE=pathlib.Path("/state")
INDEX_URL=os.environ.get("OVH_COMMAND_INDEX_URL","https://raw.githubusercontent.com/thebusinessflowtv/theofficemusic/main/control/ovh-commands/index.json")
RAW_BASE=os.environ.get("OVH_COMMAND_RAW_BASE","https://raw.githubusercontent.com/thebusinessflowtv/theofficemusic/main/")
API=os.environ.get("MEDIAFORGE_API_URL","https://mediaforge-api.guilhermeodsgn.workers.dev").rstrip("/")
POLL=max(3,int(os.environ.get("OVH_AGENT_POLL_SECONDS","5")))
SLOTS=("kick","twitch","youtube-deep-house","youtube-rainy")
PROCESSED=STATE/"agent"/"processed.json"
PROCESSED.parent.mkdir(parents=True,exist_ok=True)

def iso_now():return datetime.now(timezone.utc).isoformat().replace("+00:00","Z")
def read_json(path,default=None):
    try:return json.loads(path.read_text(encoding="utf-8"))
    except Exception:return default
def atomic_json(path,payload):
    path.parent.mkdir(parents=True,exist_ok=True)
    tmp=path.with_suffix(path.suffix+".tmp")
    tmp.write_text(json.dumps(payload,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    tmp.replace(path)
def fetch_json(url):
    req=urllib.request.Request(url+("&" if "?" in url else "?")+"ts="+str(int(time.time()*1000)),headers={"User-Agent":"MediaForge-OVH-Agent"})
    with urllib.request.urlopen(req,timeout=20) as r:return json.loads(r.read().decode("utf-8"))
def post_json(url,payload):
    data=json.dumps(payload).encode("utf-8")
    req=urllib.request.Request(url,data=data,method="POST",headers={"content-type":"application/json","user-agent":"MediaForge-OVH-Agent"})
    with urllib.request.urlopen(req,timeout=20) as r:return r.read()
def slot_for(cmd):
    slot=str(cmd.get("runtime_slot") or cmd.get("slot") or cmd.get("platform") or "")
    if slot=="youtube":slot=str(cmd.get("youtube_slot") or "")
    return slot if slot in SLOTS else ""
def next_generation(desired):
    try:return int(desired.get("generation") or 0)+1
    except Exception:return int(time.time())
def next_visual_revision(desired):
    try:return int(desired.get("visual_revision") or 0)+1
    except Exception:return int(time.time())
def apply_command(cmd):
    slot=slot_for(cmd)
    if not slot:return
    st=STATE/slot;st.mkdir(parents=True,exist_ok=True)
    desired_path=st/"desired.json"
    desired=read_json(desired_path,{}) or {}
    action=str(cmd.get("action") or "start").lower()
    tracks=cmd.get("tracks")
    if isinstance(tracks,list) and tracks:
        atomic_json(st/"playlist.json",{
            "station":slot,
            "playlist_key":str(cmd.get("playlist_key") or ""),
            "shuffle":bool(cmd.get("shuffle",True)),
            "repeat":bool(cmd.get("repeat",True)),
            "updated_at":iso_now(),
            "tracks":tracks
        })
    if action in {"skip","previous"}:
        atomic_json(st/"command.json",{
            "id": str(cmd.get("id") or uuid.uuid4()),
            "action": action,
            "requested_at": iso_now(),
            "source": "mediaforge"
        })
        return
    if action in {"update_playlist","set_playlist"}:
        desired=read_json(st/"desired.json",{}) or {}
        desired.update({
            "runtime":"ovh",
            "runtime_slot":slot,
            "session_id":str(cmd.get("session_id") or desired.get("session_id") or ""),
            "title":str(cmd.get("title") or desired.get("title") or ""),
            "playlist_key":str(cmd.get("playlist_key") or desired.get("playlist_key") or ""),
            "updated_at":iso_now(),
        })
        atomic_json(st/"desired.json",desired)
        if action=="set_playlist":
            atomic_json(st/"command.json",{
                "id": str(cmd.get("id") or uuid.uuid4()),
                "action": "skip",
                "requested_at": iso_now(),
                "source": "mediaforge-playlist-switch"
            })
        return
    if action=="set_visual":
        loop_url=str(cmd.get("loop_url") or "").strip()
        if not loop_url:
            raise ValueError("loop_url is required for set_visual")
        desired=read_json(st/"desired.json",{}) or {}
        desired.update({
            "runtime":"ovh",
            "runtime_slot":slot,
            "session_id":str(cmd.get("session_id") or desired.get("session_id") or ""),
            "title":str(cmd.get("title") or desired.get("title") or ""),
            "loop_url":loop_url,
            "desired":"live",
            # Visual changes are isolated from the RTMP session. Only the visual
            # feeder sees this revision; generation remains unchanged.
            "visual_revision":next_visual_revision(desired),
            "updated_at":iso_now(),
        })
        atomic_json(st/"desired.json",desired)
        return
    desired.update({
        "runtime":"ovh","runtime_slot":slot,
        "session_id":str(cmd.get("session_id") or desired.get("session_id") or ""),
        "title":str(cmd.get("title") or desired.get("title") or ""),
        "loop_url":str(cmd.get("loop_url") or desired.get("loop_url") or ""),
        "updated_at":iso_now(),
    })
    if action=="stop":
        desired["desired"]="stopped";desired["generation"]=next_generation(desired)
    elif action in {"start","restart","resume"}:
        desired["desired"]="live";desired["generation"]=next_generation(desired)
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
                k,v=line.split(":",1);vals[k]=int(v.strip().split()[0])
        total=vals.get("MemTotal",0);avail=vals.get("MemAvailable",0)
        if total:out["memory_percent"]=round((total-avail)*100/total,1)
    except Exception:pass
    try:
        du=shutil.disk_usage("/state");out["disk_percent"]=round((du.total-du.free)*100/du.total,1)
    except Exception:pass
    try:out["uptime_seconds"]=float(pathlib.Path("/proc/uptime").read_text().split()[0])
    except Exception:pass
    return out

def service_payload(slot):
    st=STATE/slot
    h=read_json(st/"health.json",{}) or {}
    n=read_json(st/"now-playing.json",{}) or {}
    d=read_json(st/"desired.json",{}) or {}
    return {
        "runtime_slot":slot,"platform":h.get("platform") or slot,
        "session_id":h.get("session_id") or d.get("session_id") or "",
        "title":h.get("title") or d.get("title") or "",
        "playlist_key":d.get("playlist_key") or "",
        "status":h.get("status") or ("live" if d.get("desired")=="live" else "unknown"),
        "fps":h.get("fps"),"video_bitrate_kbps":h.get("video_bitrate_kbps"),
        "restarts":h.get("restarts",0),"updated_at":h.get("updated_at"),
        "loop_url":h.get("loop_url") or d.get("loop_url") or "",
        "visual_revision":d.get("visual_revision") or 0,
        "hot_swap":bool(h.get("hot_swap",False)),
        "now_playing":n,
    }

def report(processed_count=0,last_command=None):
    payload={"agent_id":"ovh-main","runtime":"ovh","reported_at":iso_now(),"host":host_metrics(),
             "services":{s:service_payload(s) for s in SLOTS},"processed_commands":processed_count}
    if last_command:payload["last_command"]=last_command
    try:post_json(API+"/api/ovh/agent/status",payload)
    except Exception as exc:print("status report failed:",exc,flush=True)

def main():
    processed=set(read_json(PROCESSED,[]) or [])
    last_report=0
    while True:
        last_cmd=None
        api_ok=False

        # Primary path: reliable Cloudflare D1 command queue.
        try:
            batch=fetch_json(API+"/api/ovh/agent/commands?limit=20")
            api_ok=True
            for cmd in batch.get("commands") or []:
                cid=str(cmd.get("id") or "")
                if not cid:
                    continue
                try:
                    if cid not in processed:
                        apply_command(cmd)
                        processed.add(cid)
                        atomic_json(PROCESSED,sorted(processed)[-500:])
                    last_cmd={"id":cid,"action":cmd.get("action"),"runtime_slot":slot_for(cmd),"processed_at":iso_now(),"transport":"d1"}
                    post_json(API+"/api/ovh/agent/command-ack",{"id":cid,"status":"completed"})
                    print("processed",last_cmd,flush=True)
                except Exception as exc:
                    try:post_json(API+"/api/ovh/agent/command-ack",{"id":cid,"status":"failed","error":str(exc)[:500]})
                    except Exception:pass
                    print("command apply failed",cid,exc,flush=True)
        except Exception as exc:
            print("d1 command poll failed:",exc,flush=True)

        # Fallback/audit path: public GitHub queue.
        try:
            idx=fetch_json(INDEX_URL)
            for item in idx.get("commands") or []:
                cid=str(item.get("id") or "")
                if not cid or cid in processed:continue
                path=str(item.get("path") or f"control/ovh-commands/{cid}.json")
                cmd=fetch_json(RAW_BASE+path)
                apply_command(cmd)
                processed.add(cid)
                last_cmd={"id":cid,"action":cmd.get("action"),"runtime_slot":slot_for(cmd),"processed_at":iso_now(),"transport":"github-fallback"}
                atomic_json(PROCESSED,sorted(processed)[-500:])
                print("processed",last_cmd,flush=True)
        except Exception as exc:
            if not api_ok:
                print("github command poll failed:",exc,flush=True)

        now=time.time()
        if now-last_report>=10:
            report(len(processed),last_cmd);last_report=now
        time.sleep(POLL)

if __name__=="__main__":main()
