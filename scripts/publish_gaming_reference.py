#!/usr/bin/env python3
import argparse
import json
import pathlib
from datetime import datetime, timezone
from urllib.parse import quote

ROOT = pathlib.Path(".")
STATION = ROOT / "ovh-streaming" / "stations" / "gaming.json"
PROD = ROOT / "control" / "gaming-reference-production"


def now():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def read(path, default=None):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def manifest_title(generated_dir):
    files = list(pathlib.Path(generated_dir).glob("*_manifest.json"))
    if not files:
        return None, None
    d = read(files[0], {})
    tracks = d.get("tracks") or []
    if not tracks:
        return None, d
    return tracks[0].get("title"), d


def active_targets():
    out = []
    for platform in ("kick", "twitch"):
        p = ROOT / "control" / f"{platform}-active.json"
        d = read(p, {})
        if not isinstance(d, dict):
            continue
        sid = str(d.get("session_id") or "").strip()
        status = str(d.get("status") or "").lower()
        if sid and status in {"live", "starting", "healthy", "active"}:
            out.append((platform, sid, d))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--index", type=int, required=True)
    ap.add_argument("--attempt", type=int, required=True)
    ap.add_argument("--release-tag", required=True)
    ap.add_argument("--mp3-name", required=True)
    ap.add_argument("--reference-profile", required=True)
    ap.add_argument("--qc", required=True)
    ap.add_argument("--generated-dir", default="generated")
    args = ap.parse_args()

    ref = read(pathlib.Path(args.reference_profile), {})
    qc = read(pathlib.Path(args.qc), {})
    if not qc.get("approved"):
        raise SystemExit("Refusing to publish a track that did not pass Quality Gate")

    title, manifest = manifest_title(args.generated_dir)
    if not title:
        title = (ref.get("derived_traits") or {}).get("title") or f"Gaming Reference {args.index:03d}"

    station = read(STATION, {})
    tracks = station.get("tracks") or []
    track_id = f"gaming-radio-ref-{args.index:03d}"

    existing = next((x for x in tracks if x.get("id") == track_id), None)
    if existing:
        print(f"{track_id} already published; keeping idempotent state")
        new_track = existing
    else:
        url = (
            "https://github.com/thebusinessflowtv/theofficemusic/releases/download/"
            + quote(args.release_tag, safe="-._")
            + "/"
            + quote(args.mp3_name, safe="-._")
        )
        new_track = {
            "id": track_id,
            "title": title,
            "url": url,
            "position": len(tracks) + 1,
            "duration_seconds": 300,
            "source": "gaming-reference-215",
            "reference_position": args.index,
            "quality_gate": "approved",
        }
        tracks.append(new_track)

    for i, t in enumerate(tracks, start=1):
        t["position"] = i

    station.update({
        "station": "gaming",
        "playlist_key": "gaming-radio",
        "shuffle": True,
        "repeat": True,
        "updated_at": now(),
        "tracks": tracks,
    })
    write(STATION, station)

    targets = active_targets()
    updated_sessions = []
    for platform, sid, active in targets:
        live_path = ROOT / "control" / "live-playlists" / f"{sid}.json"
        live = read(live_path, {})
        current_key = str(live.get("playlist_key") or "gaming-radio")
        title_live = str(active.get("title") or live.get("title") or "")
        is_gaming = current_key == "gaming-radio" or "gaming" in title_live.lower()
        if not is_gaming:
            continue

        live.update({
            "session_id": sid,
            "platform": platform,
            "runtime": "ovh",
            "runtime_slot": str(active.get("runtime_slot") or platform),
            "playlist_key": "gaming-radio",
            "shuffle": True,
            "repeat": True,
            "updated_at": now(),
            "tracks": tracks,
        })
        write(live_path, live)
        updated_sessions.append(f"{platform}:{sid}")

        cmd_id = f"gaming-ref-{args.index:03d}-{platform}"
        cmd = {
            "id": cmd_id,
            "action": "update_playlist",
            "platform": platform,
            "runtime": "ovh",
            "runtime_slot": str(active.get("runtime_slot") or platform),
            "session_id": sid,
            "title": title_live,
            "playlist_key": "gaming-radio",
            "tracks": tracks,
            "shuffle": True,
            "repeat": True,
            "requested_at": now(),
            "source": "gaming-reference-auto-publish",
        }
        write(ROOT / "control" / "ovh-commands" / f"{cmd_id}.json", cmd)

    reference = ref.get("reference") or {}
    result = {
        "status": "approved_published",
        "reference_index": args.index,
        "attempt": args.attempt,
        "published_at": now(),
        "reference": {
            "spotify_id": reference.get("spotify_id"),
            "spotify_url": reference.get("spotify_url"),
            "title": reference.get("title"),
            "artists": reference.get("artists"),
            "copyright_status": reference.get("copyright_status"),
            "copyright_basis": reference.get("copyright_basis"),
        },
        "reference_analysis": {
            "feature_source": ref.get("feature_source"),
            "audio_features": ref.get("audio_features"),
            "derived_traits": ref.get("derived_traits"),
            "source_audio_used": False,
            "artist_or_title_in_generation_prompt": False,
        },
        "generated": {
            "id": new_track.get("id"),
            "title": new_track.get("title"),
            "url": new_track.get("url"),
            "duration_seconds": 300,
            "release_tag": args.release_tag,
            "source_wav_sha256": qc.get("source_wav_sha256"),
            "model": "stable-audio-3-medium",
            "pure_text_to_audio": True,
        },
        "quality_gate": qc,
        "live_delivery": {
            "station_track_count": len(tracks),
            "updated_active_sessions": updated_sessions,
            "method": "playlist_update_only_no_stream_restart",
        },
    }
    write(PROD / f"{args.index:03d}.json", result)

    completed = []
    for p in sorted(PROD.glob("[0-9][0-9][0-9].json")):
        d = read(p, {})
        if d.get("status") == "approved_published":
            completed.append(int(d.get("reference_index") or int(p.stem)))

    status = {
        "status": "running" if len(completed) < 215 else "completed",
        "total": 215,
        "approved_published": len(set(completed)),
        "last_completed_index": args.index,
        "next_index": args.index + 1 if args.index < 215 else None,
        "gaming_station_track_count": len(tracks),
        "updated_at": now(),
        "policy": "sequential: reference -> rights flag -> feature analysis -> generate -> quality gate -> publish -> next",
    }
    write(PROD / "status.json", status)
    print(json.dumps(status, ensure_ascii=False))


if __name__ == "__main__":
    main()
