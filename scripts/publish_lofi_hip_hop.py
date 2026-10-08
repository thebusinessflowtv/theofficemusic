#!/usr/bin/env python3
"""Publish one QC-approved Lofi Hip Hop track to GitHub and OVH music catalog."""
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen

ROOT=Path("control/lofi-hip-hop")
PLAYLIST_KEY="lofi-hip-hop"

def append_track(library,track):
    playlists=library.setdefault("playlists",[])
    playlist=next((p for p in playlists if p.get("key")==PLAYLIST_KEY),None)
    if playlist is None:
        playlist={
            "key":PLAYLIST_KEY,"name":"Lofi Hip Hop","category":"Live Radio","series":"Lofi Hip Hop",
            "genre":"Instrumental Lofi Hip Hop / Jazzy Boom Bap",
            "moods":["warm","jazzy","chill","relaxed","focused"],
            "source":"peter-lofi-lofi-hip-hop-production",
            "tracks":[]
        }
        playlists.append(playlist)
    tracks=playlist.setdefault("tracks",[])
    if not any(x.get("id")==track["id"] for x in tracks):
        tracks.append(track)
    tracks.sort(key=lambda x:int(x.get("position",9999)))
    playlist["track_count"]=len(tracks)
    playlist["total_duration_seconds"]=sum(int(x.get("duration_seconds",0)) for x in tracks)
    playlist["planned_track_count"]=36
    playlist["planned_duration_seconds"]=10800
    playlist["target_duration_seconds_per_track"]=300
    playlist["status"]="complete" if len(tracks)>=36 else "generating"
    library["updated_at"]=datetime.now(timezone.utc).isoformat()
    return library

def main():
    idx=int(os.environ["INDEX"])
    queue=json.loads((ROOT/"queue.json").read_text(encoding="utf-8"))
    item=next(x for x in queue["tracks"] if x["index"]==idx)
    qc=json.loads(Path("build/qc.json").read_text(encoding="utf-8"))
    diversity=json.loads(Path("build/diversity.json").read_text(encoding="utf-8"))
    if not qc.get("approved") or not diversity.get("approved"):
        raise RuntimeError("Refusing to publish music that failed quality/diversity checks")
    url=(f"https://github.com/{os.environ['GITHUB_REPOSITORY']}/releases/download/"
         f"{os.environ['RELEASE_TAG']}/{os.environ['MP3_NAME']}")
    track={
        "id":item["id"],"title":item["title"],"url":url,
        "duration_seconds":300,"position":item["playlist_position"],
        "source":"lofi-hip-hop-original","target_bpm":item["target_bpm"],
        "intro_identity":item["intro_identity"],
        "quality_gate":"technical_and_45s_intro_diversity_passed"
    }
    file=Path("control/music-library.json")
    library=json.loads(file.read_text(encoding="utf-8"))
    file.write_text(json.dumps(append_track(library,track),ensure_ascii=False,indent=2)+"\n",encoding="utf-8")

    delivery="pending_ovh_credentials"
    token=os.environ.get("OVH_AGENT_TOKEN","").strip()
    if token:
        base="https://peterlofi.odsgn.com.br/api/ovh/agent/runtime-config"
        headers={"x-ovh-agent-token":token,"content-type":"application/json"}
        try:
            query=urlencode({"path":"control/music-library.json","raw":"1"})
            with urlopen(Request(base+"?"+query,headers=headers),timeout=60) as r:
                remote=json.load(r)
            payload={"path":"control/music-library.json","payload":append_track(remote,track)}
            with urlopen(Request(base,data=json.dumps(payload).encode(),headers=headers,method="POST"),timeout=60) as r:
                if not json.load(r).get("ok"): raise RuntimeError("OVH did not acknowledge catalog update")
            delivery="added_to_ovh_library"
        except Exception as exc:
            delivery="pending_ovh_retry"
            print("OVH catalog delivery pending:",type(exc).__name__,str(exc)[:180])
    result={
        "index":idx,"status":"generated","track":track,"delivery":delivery,
        "quality_gate":qc,"diversity_gate":diversity,
        "github_run_id":os.environ["GITHUB_RUN_ID"],
        "generated_at":datetime.now(timezone.utc).isoformat()
    }
    (ROOT/f"{idx:02d}.json").write_text(json.dumps(result,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    print(json.dumps({"index":idx,"title":item["title"],"delivery":delivery,"status":"generated"}))

if __name__=="__main__": main()
