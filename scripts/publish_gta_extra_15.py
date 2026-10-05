import json
import os
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen

QUEUE_ROOT = Path("control/gta-vi-extra-15")
PLAYLIST_KEY = "gta-vi-vice-city"

def append_track(library, track):
    playlist = next((p for p in library["playlists"] if p.get("key") == PLAYLIST_KEY), None)
    if playlist is None:
        raise RuntimeError("GTA VI - Vice City playlist missing")
    tracks = playlist.setdefault("tracks", [])
    if not any(t.get("id") == track["id"] for t in tracks):
        tracks.append(track)
    tracks.sort(key=lambda t: int(t.get("position", 9999)))
    playlist["track_count"] = len(tracks)
    playlist["total_duration_seconds"] = sum(int(t.get("duration_seconds", 0)) for t in tracks)
    playlist["planned_track_count"] = 55
    playlist["generated_target_count"] = 50
    playlist["owner_library_track_count"] = 5
    playlist["planned_duration_seconds"] = 16500
    playlist["target_duration_seconds_per_track"] = 300
    playlist["status"] = "expanding_with_reference_families"
    library["updated_at"] = datetime.now(timezone.utc).isoformat()
    return library

if __name__ == "__main__":
    index = int(os.environ["INDEX"])
    queue = json.loads((QUEUE_ROOT / "queue.json").read_text(encoding="utf-8"))
    item = next(x for x in queue["tracks"] if int(x["index"]) == index)
    qc = json.loads(Path("build/qc.json").read_text(encoding="utf-8"))
    diversity = json.loads(Path("build/diversity.json").read_text(encoding="utf-8"))
    if not qc.get("approved") or not diversity.get("approved"):
        raise SystemExit("Track not approved by QC/diversity gate")

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
        "source": "gta-vi-extra-15",
        "family": item["family"],
        "reference_title": item["reference_title"],
        "quality_gate": "technical_and_diversity_checks_passed",
        "target_bpm": item["target_bpm"],
    }

    path = Path("control/music-library.json")
    library = json.loads(path.read_text(encoding="utf-8"))
    path.write_text(json.dumps(append_track(library, track), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    delivery = "pending_ovh_credentials"
    token = os.environ.get("OVH_AGENT_TOKEN", "")
    if token:
        base = "https://peterlofi.odsgn.com.br/api/ovh/agent/runtime-config"
        headers = {"x-ovh-agent-token": token, "content-type": "application/json"}
        try:
            query = urlencode({"path": "control/music-library.json", "raw": "1"})
            with urlopen(Request(base + "?" + query, headers=headers), timeout=60) as response:
                remote_library = json.load(response)
            payload = {"path": "control/music-library.json", "payload": append_track(remote_library, track)}
            with urlopen(Request(base, data=json.dumps(payload).encode(), headers=headers, method="POST"), timeout=60) as response:
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
        "diversity_gate": diversity,
        "run_id": os.environ["GITHUB_RUN_ID"],
    }
    (QUEUE_ROOT / f"{index:02d}.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"index": index, "title": item["title"], "family": item["family"], "delivery": delivery}))
