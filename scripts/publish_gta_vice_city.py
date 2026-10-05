import json
import os
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen

ROOT = Path("control/gta-vi-vice-city")
PLAYLIST_KEY = "gta-vi-vice-city"
PLAYLIST_NAME = "GTA VI - Vice City"

OWNER_TRACKS = [
    {"title": "Digifunk", "artist": "DivKid", "filename": "Digifunk - DivKid.mp3", "source_duration_seconds": 192.0, "target_duration_seconds": 300, "ingest_processing": "extend_or_restructure_to_exact_300s_then_loudness_normalize"},
    {"title": "Icelandic Arpeggios", "artist": "DivKid", "filename": "Icelandic Arpeggios - DivKid.mp3", "source_duration_seconds": 170.0, "target_duration_seconds": 300, "ingest_processing": "extend_or_restructure_to_exact_300s_then_loudness_normalize"},
    {"title": "No One Here Gets In Alive", "artist": "National Sweetheart", "filename": "No One Here Gets In Alive - National Sweetheart.mp3", "source_duration_seconds": 238.4, "target_duration_seconds": 300, "ingest_processing": "extend_or_restructure_to_exact_300s_then_loudness_normalize"},
    {"title": "Rinse Repeat", "artist": "DivKid", "filename": "Rinse Repeat - DivKid.mp3", "source_duration_seconds": 156.0, "target_duration_seconds": 300, "ingest_processing": "extend_or_restructure_to_exact_300s_then_loudness_normalize"},
    {"title": "Visions", "artist": "Patrick Jordan Patrikios", "filename": "Visions - Patrick Jordan Patrikios.mp3", "source_duration_seconds": 148.0, "target_duration_seconds": 300, "ingest_processing": "extend_or_restructure_to_exact_300s_then_loudness_normalize"},
]


def ensure_playlist(library):
    playlist = next((p for p in library["playlists"] if p.get("key") == PLAYLIST_KEY), None)
    if playlist is None:
        playlist = {
            "key": PLAYLIST_KEY,
            "name": PLAYLIST_NAME,
            "category": "Series",
            "series": "GTA VI - Vice City",
            "genre": "Retro synthwave / electro-funk / neon night-drive",
            "moods": ["neon", "nocturnal", "coastal", "driving"],
            "source": "gta-vi-vice-city-production",
            "planned_track_count": 40,
            "planned_duration_seconds": 12000,
            "target_duration_seconds_per_track": 300,
            "owner_library_tracks": OWNER_TRACKS,
            "track_count": 0,
            "total_duration_seconds": 0,
            "tracks": [],
        }
        library["playlists"].append(playlist)
    else:
        playlist["planned_track_count"] = 40
        playlist["owner_library_tracks"] = OWNER_TRACKS
        playlist.setdefault("tracks", [])
    return playlist


def append_generated(library, track):
    playlist = ensure_playlist(library)
    tracks = playlist["tracks"]
    if not any(t.get("id") == track["id"] for t in tracks):
        tracks.append(track)
    tracks.sort(key=lambda t: int(t.get("position", 9999)))
    playlist["track_count"] = len(tracks)
    playlist["total_duration_seconds"] = sum(int(t.get("duration_seconds", 0)) for t in tracks)
    library["updated_at"] = datetime.now(timezone.utc).isoformat()
    return library


if __name__ == "__main__":
    index = int(os.environ["INDEX"])
    item = json.loads((ROOT / "queue.json").read_text(encoding="utf-8"))["generated_tracks"][index - 1]
    qc = json.loads(Path("build/qc.json").read_text(encoding="utf-8"))
    if not qc.get("approved"):
        raise SystemExit("QC not approved")

    url = (
        f"https://github.com/{os.environ['GITHUB_REPOSITORY']}/releases/download/"
        f"{os.environ['RELEASE_TAG']}/{os.environ['MP3_NAME']}"
    )
    track = {
        "id": item["id"],
        "title": item["title"],
        "url": url,
        "duration_seconds": 300,
        "position": int(item["playlist_position"]),
        "source": "gta-vi-vice-city",
        "quality_gate": "technical_checks_passed",
        "target_bpm": item["target_bpm"],
    }

    path = Path("control/music-library.json")
    library = json.loads(path.read_text(encoding="utf-8"))
    path.write_text(json.dumps(append_generated(library, track), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    delivery = "pending_ovh_credentials"
    token = os.environ.get("OVH_AGENT_TOKEN", "")
    if token:
        base = "https://peterlofi.odsgn.com.br/api/ovh/agent/runtime-config"
        headers = {"x-ovh-agent-token": token, "content-type": "application/json"}
        try:
            query = urlencode({"path": "control/music-library.json", "raw": "1"})
            with urlopen(Request(base + "?" + query, headers=headers), timeout=60) as response:
                remote_library = json.load(response)
            payload = {
                "path": "control/music-library.json",
                "payload": append_generated(remote_library, track),
            }
            with urlopen(
                Request(
                    base,
                    data=json.dumps(payload).encode(),
                    headers=headers,
                    method="POST",
                ),
                timeout=60,
            ) as response:
                if not json.load(response).get("ok"):
                    raise RuntimeError("OVH did not acknowledge")
            delivery = "added_to_ovh_library"
        except Exception as exc:
            delivery = "pending_ovh_retry"
            print("OVH delivery pending:", type(exc).__name__)

    result = {
        "index": index,
        "status": "generated",
        "track": track,
        "delivery": delivery,
        "quality_gate": qc,
        "run_id": os.environ["GITHUB_RUN_ID"],
    }
    (ROOT / f"{index:02d}.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"index": index, "title": item["title"], "delivery": delivery}))
