#!/usr/bin/env python3
import json
import pathlib
import re
from datetime import datetime, timezone

ROOT=pathlib.Path(__file__).resolve().parents[1]
RESULTS=ROOT/"control"/"series-results"
CONFIG=ROOT/"config"/"peter_lofi_series.json"
OUT=ROOT/"control"/"music-library.json"
STATIONS={
    "gaming-radio": ROOT/"ovh-streaming"/"stations"/"gaming.json",
    "deep-house-radio": ROOT/"ovh-streaming"/"stations"/"youtube-deep-house.json",
    "rainy-radio": ROOT/"ovh-streaming"/"stations"/"youtube-rainy.json",
}
LIVE_META={
    "gaming-radio":{"name":"Gaming Radio","series":"Gaming","genre":"Gaming Lofi / Focus","moods":["gaming","focused","neon"]},
    "deep-house-radio":{"name":"Deep House Radio","series":"Deep House","genre":"Deep House / Lounge / Focus","moods":["focused","polished","lounge"]},
    "rainy-radio":{"name":"Rainy Lofi Radio","series":"Rainy + Late Night Office","genre":"Rainy Lofi / Late Night Lofi","moods":["rainy","cozy","nocturnal","focused"]},
}

def now():
    return datetime.now(timezone.utc).isoformat().replace("+00:00","Z")

def clean_title(value):
    name=str(value or "").split("/")[-1]
    name=re.sub(r"\.(wav|mp3|m4a)$","",name,flags=re.I)
    name=re.sub(r"^\d{2}(?:-\d{2})?(?:-s\d+)?(?:-\d{2})?-?","",name,flags=re.I)
    name=re.sub(r"[-_]+"," ",name)
    return " ".join(x.capitalize() for x in name.split()) or "Track"

def normalize_tracks(key,items):
    out=[]
    for i,t in enumerate(items or [],1):
        if not isinstance(t,dict):
            continue
        url=t.get("download_url") or t.get("url")
        if not url:
            continue
        out.append({
            "id":str(t.get("id") or f"{key}-{i:02d}"),
            "title":str(t.get("display_title") or clean_title(t.get("title") or t.get("filename") or url)),
            "url":str(url),
            "duration_seconds":float(t.get("duration_seconds") or 300),
            "position":i,
        })
    return out

config=json.loads(CONFIG.read_text(encoding="utf-8")) if CONFIG.exists() else {"series":[]}
meta_by_key={x.get("key"):x for x in config.get("series",[])}

playlists=[]

# Live playlists first: they are the authoritative runtime definitions.
for key,path in STATIONS.items():
    if not path.exists():
        continue
    d=json.loads(path.read_text(encoding="utf-8"))
    tracks=normalize_tracks(key,d.get("tracks"))
    if not tracks:
        continue
    m=LIVE_META[key]
    playlists.append({
        "key":key,
        "name":m["name"],
        "category":"Live Radio",
        "series":m["series"],
        "genre":m["genre"],
        "moods":m["moods"],
        "source":"ovh-station",
        "track_count":len(tracks),
        "total_duration_seconds":round(sum(x["duration_seconds"] for x in tracks)),
        "tracks":tracks,
    })

# Generated series. Master mixes remain metadata only; lives consume tracks[].
for path in sorted(RESULTS.glob("*.json")):
    try:
        d=json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        continue
    key=str(d.get("key") or path.stem)
    tracks=normalize_tracks(key,d.get("tracks"))
    if not tracks:
        continue
    meta=meta_by_key.get(key) or {}
    dna=meta.get("music_dna") or {}
    genre=(dna.get("style_pool") or [None])[0]
    if not genre:
        if "gaming" in key: genre="Gaming Lofi / Focus"
        elif "coffee" in key: genre="Coffee Shop Lofi"
        elif "rainy" in key: genre="Rainy Lofi"
        elif "coding" in key: genre="Deep Focus Lofi"
        elif "luxury" in key: genre="Deep House / Lounge"
        else: genre="Lofi"
    playlists.append({
        "key":key,
        "name":str(d.get("name") or meta.get("name") or key),
        "category":"Long Series" if int(d.get("duration_minutes") or 0)>=180 else "Series",
        "series":str(meta.get("name") or d.get("name") or key),
        "genre":genre,
        "moods":dna.get("mood") or [],
        "source":"series-result",
        "release_tag":d.get("release_tag"),
        "master_audio_url":d.get("master_audio_url"),
        "track_count":len(tracks),
        "total_duration_seconds":round(sum(x["duration_seconds"] for x in tracks)),
        "tracks":tracks,
    })

payload={
    "version":1,
    "brand":"Peter Lofi",
    "updated_at":now(),
    "rules":{
        "live_playback":"individual_tracks_only",
        "masters_for":"long_form_video_exports_only",
        "repeat":"continuous",
        "shuffle_default":True,
        "platform_state":"independent_per_runtime_slot",
    },
    "categories":[
        {"key":"live-radio","name":"Live Radio"},
        {"key":"series","name":"Series"},
        {"key":"long-series","name":"Long Series"},
    ],
    "playlists":playlists,
}
OUT.parent.mkdir(parents=True,exist_ok=True)
OUT.write_text(json.dumps(payload,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
print(json.dumps({"playlists":len(playlists),"tracks":sum(x["track_count"] for x in playlists)},ensure_ascii=False))
