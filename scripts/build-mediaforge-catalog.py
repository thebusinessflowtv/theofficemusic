#!/usr/bin/env python3
import json
import hashlib
from pathlib import Path
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parents[1]
RESULTS = ROOT / "control" / "results"
SERIES_RESULTS = ROOT / "control" / "series-results"
OUT = ROOT / "control" / "mediaforge-catalog.json"


def stable_id(*parts: str) -> str:
    raw = "::".join(str(p or "") for p in parts)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


tracks = []
jobs = []
series = []
hour_mixes = []

for path in sorted(RESULTS.glob("*.json")):
    data = read_json(path)
    if not isinstance(data, dict) or data.get("status") != "completed":
        continue
    job_id = path.stem
    item_tracks = []
    for t in data.get("tracks") or []:
        url = t.get("download_url") or t.get("github_path")
        if not url:
            continue
        tid = stable_id("job", job_id, t.get("filename") or t.get("title") or url)
        item = {
            "id": tid,
            "source": "office_music_job",
            "job_id": job_id,
            "series_key": None,
            "collection_key": f"job:{job_id}",
            "collection_name": f"Mix {data.get('requested_duration_minutes') or ''} min".strip(),
            "title": t.get("title") or t.get("filename") or tid,
            "filename": t.get("filename"),
            "duration_seconds": int(float(t.get("duration_seconds") or 0)),
            "position": int(t.get("position") or 0),
            "style": t.get("style"),
            "url": url,
            "created_at": data.get("completed_at"),
            "metadata": {
                "release_tag": data.get("release_tag") or t.get("github_release_tag"),
                "youtube_video_id": data.get("youtube_video_id"),
                "youtube_url": data.get("youtube_url"),
                "generation_mode": data.get("generation_mode"),
                "sha256": t.get("sha256"),
            },
        }
        tracks.append(item)
        item_tracks.append(tid)
    jobs.append({
        "id": job_id,
        "status": data.get("status"),
        "requested_duration_minutes": int(data.get("requested_duration_minutes") or 0),
        "youtube_video_id": data.get("youtube_video_id"),
        "youtube_url": data.get("youtube_url"),
        "release_tag": data.get("release_tag"),
        "completed_at": data.get("completed_at"),
        "track_ids": item_tracks,
    })
    total_seconds = sum(t["duration_seconds"] for t in tracks if t.get("job_id") == job_id)
    if int(data.get("requested_duration_minutes") or 0) >= 55 or total_seconds >= 3300:
        hour_mixes.append({
            "id": f"job:{job_id}",
            "job_id": job_id,
            "kind": "assembled_mix",
            "name": f"Mix de 1 hora · {data.get('completed_at','')[:10]}",
            "duration_seconds": total_seconds,
            "track_ids": item_tracks,
            "youtube_video_id": data.get("youtube_video_id"),
            "youtube_url": data.get("youtube_url"),
        })

for path in sorted(SERIES_RESULTS.glob("*.json")):
    data = read_json(path)
    if not isinstance(data, dict) or data.get("status") != "completed":
        continue
    key = str(data.get("key") or path.stem)
    request_id = str(data.get("request_id") or key)
    series_name = data.get("name") or key
    item_tracks = []
    for t in data.get("tracks") or []:
        url = t.get("download_url")
        if not url:
            continue
        tid = stable_id("series", request_id, t.get("filename") or t.get("title") or url)
        item = {
            "id": tid,
            "source": "peter_lofi_series",
            "job_id": None,
            "series_key": key,
            "collection_key": f"series:{key}",
            "collection_name": series_name,
            "title": t.get("title") or t.get("filename") or tid,
            "filename": t.get("filename"),
            "duration_seconds": int(float(t.get("duration_seconds") or 0)),
            "position": int(t.get("position") or 0),
            "style": f"Peter Lofi · {series_name}",
            "url": url,
            "created_at": data.get("completed_at"),
            "metadata": {
                "release_tag": data.get("release_tag"),
                "series_index": data.get("index"),
                "series_name": data.get("name"),
                "series_title": data.get("title"),
                "series_playlist": data.get("playlist"),
                "master_audio_url": data.get("master_audio_url"),
                "generation_mode": data.get("generation_mode"),
                "request_id": request_id,
                "sha256": t.get("sha256"),
            },
        }
        tracks.append(item)
        item_tracks.append(tid)

    master_url = str(data.get("master_audio_url") or "").strip()
    master_track_id = None
    if master_url:
        master_track_id = stable_id("series-master", request_id, master_url)
        master_seconds = int(data.get("duration_minutes") or 60) * 60
        tracks.append({
            "id": master_track_id,
            "source": "peter_lofi_master",
            "job_id": None,
            "series_key": key,
            "collection_key": f"master:{key}",
            "collection_name": f"{series_name} · Master 1 hora",
            "title": f"{series_name} — Master 1 Hour",
            "filename": "peter-lofi-master.m4a",
            "duration_seconds": master_seconds,
            "position": 1,
            "style": f"Peter Lofi · {series_name} · 1 Hour Master",
            "url": master_url,
            "created_at": data.get("completed_at"),
            "metadata": {
                "release_tag": data.get("release_tag"),
                "series_index": data.get("index"),
                "series_name": data.get("name"),
                "series_title": data.get("title"),
                "series_playlist": data.get("playlist"),
                "generation_mode": data.get("generation_mode"),
                "request_id": request_id,
                "is_master_audio": True,
            },
        })
        hour_mixes.append({
            "id": f"master:{key}",
            "job_id": None,
            "series_key": key,
            "kind": "master_audio",
            "name": f"{series_name} · Master pronto de 1 hora",
            "duration_seconds": master_seconds,
            "track_ids": [master_track_id],
            "master_audio_url": master_url,
        })

    series.append({
        "id": f"series:{key}",
        "key": key,
        "index": data.get("index"),
        "name": series_name,
        "title": data.get("title"),
        "playlist": data.get("playlist"),
        "description": data.get("description"),
        "duration_minutes": int(data.get("duration_minutes") or 0),
        "release_tag": data.get("release_tag"),
        "release_url": data.get("release_url"),
        "master_audio_url": data.get("master_audio_url"),
        "completed_at": data.get("completed_at"),
        "track_ids": item_tracks,
        "master_track_id": master_track_id,
    })

tracks.sort(key=lambda t: (t.get("created_at") or "", t.get("collection_key") or "", t.get("position") or 0))
series.sort(key=lambda s: (s.get("index") or 999, s.get("name") or ""))
hour_mixes.sort(key=lambda m: (0 if m.get("kind") == "master_audio" else 1, m.get("name") or ""))

catalog = {
    "version": 2,
    "generated_at": datetime.now(timezone.utc).isoformat(),
    "counts": {
        "tracks": len(tracks),
        "jobs": len(jobs),
        "series": len(series),
        "hour_mixes": len(hour_mixes),
        "one_hour_masters": len([m for m in hour_mixes if m.get("kind") == "master_audio"]),
    },
    "tracks": tracks,
    "jobs": jobs,
    "series": series,
    "hour_mixes": hour_mixes,
}
OUT.parent.mkdir(parents=True, exist_ok=True)
OUT.write_text(json.dumps(catalog, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
print(json.dumps(catalog["counts"], ensure_ascii=False))
