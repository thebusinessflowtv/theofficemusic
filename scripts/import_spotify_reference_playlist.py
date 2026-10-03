#!/usr/bin/env python3
import concurrent.futures
import json
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import requests

PLAYLIST_URL = "https://open.spotify.com/playlist/20VbdrwgklKralOrhZ1BNZ"
PLAYLIST_ID = "20VbdrwgklKralOrhZ1BNZ"
EXPECTED_TRACKS = 215
OUT = Path("control/gaming-reference-production/references.json")
EMBED_PLAYLIST = f"https://open.spotify.com/embed/playlist/{PLAYLIST_ID}"
SPCLIENT = f"https://spclient.wg.spotify.com/playlist/v2/playlist/{PLAYLIST_ID}"
NEXT_DATA_RE = re.compile(r'<script id="__NEXT_DATA__"[^>]*>([^<]+)</script>')
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/154 Safari/537.36"

session = requests.Session()
session.headers.update({"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"})


def now():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def resolve_path(data, path):
    cur = data
    for key in path:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(key)
    return cur


def deep_find(data, key, depth=7):
    if depth <= 0:
        return None
    if isinstance(data, dict):
        if key in data:
            return data
        for value in data.values():
            found = deep_find(value, key, depth - 1)
            if found is not None:
                return found
    elif isinstance(data, list):
        for value in data[:20]:
            found = deep_find(value, key, depth - 1)
            if found is not None:
                return found
    return None


def fetch_embed(url):
    last = None
    for attempt in range(4):
        try:
            r = session.get(url, timeout=35)
            if r.status_code == 429:
                time.sleep(2 ** attempt)
                continue
            r.raise_for_status()
            match = NEXT_DATA_RE.search(r.text)
            if not match:
                raise RuntimeError(f"Spotify embed did not expose __NEXT_DATA__: {url}")
            return json.loads(match.group(1))
        except Exception as exc:
            last = exc
            if attempt < 3:
                time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"Spotify embed fetch failed: {url}: {last}")


def extract_entity(data):
    for path in (
        ("props", "pageProps", "state", "data", "entity"),
        ("props", "pageProps", "data", "entity"),
        ("props", "pageProps", "entity"),
    ):
        entity = resolve_path(data, path)
        if isinstance(entity, dict):
            return entity
    found = deep_find(data, "trackList")
    if isinstance(found, dict):
        return found
    found = deep_find(data, "uri")
    if isinstance(found, dict):
        return found
    raise RuntimeError("Could not locate Spotify entity payload")


def extract_session_token(data):
    candidates = (
        ("props", "pageProps", "state", "settings", "session"),
        ("props", "pageProps", "settings", "session"),
        ("props", "pageProps", "session"),
    )
    for path in candidates:
        value = resolve_path(data, path)
        if isinstance(value, dict) and value.get("accessToken"):
            return str(value["accessToken"])
    found = deep_find(data, "accessToken")
    if isinstance(found, dict) and found.get("accessToken"):
        return str(found["accessToken"])
    return None


def spotify_track_id(track):
    uri = str(track.get("uri") or "")
    if uri.startswith("spotify:track:"):
        return uri.rsplit(":", 1)[-1]
    link = str(track.get("link") or "")
    m = re.search(r"/track/([A-Za-z0-9]{22})", link)
    return m.group(1) if m else ""


def normalize_duration(value):
    if isinstance(value, dict):
        value = value.get("totalMilliseconds") or value.get("milliseconds")
    try:
        n = float(value)
    except Exception:
        return None
    if n <= 0:
        return None
    return int(round(n if n > 10000 else n * 1000))


def parse_track(track, position):
    tid = spotify_track_id(track)
    if not tid:
        return None
    title = str(track.get("title") or track.get("name") or "Unknown Track").strip()
    artists = track.get("subtitle") or track.get("artists") or ""
    if isinstance(artists, list):
        artists = ", ".join(
            str(a.get("name") if isinstance(a, dict) else a).strip()
            for a in artists
            if str(a).strip()
        )
    artists = str(artists).strip()
    preview = track.get("audioPreview")
    if isinstance(preview, dict):
        preview = preview.get("url")
    return {
        "position": int(position),
        "spotify_id": tid,
        "spotify_uri": f"spotify:track:{tid}",
        "spotify_url": f"https://open.spotify.com/track/{tid}",
        "title": title,
        "artists": artists,
        "duration_ms": normalize_duration(track.get("duration")),
        "copyright_status": "presumed_copyrighted",
        "copyright_basis": "Commercial Spotify catalog reference; no explicit public-domain or open-license evidence.",
        "allowed_reference_use": "metadata_and_nonexpressive_audio_features_only",
    }


def fetch_track_meta(track_id, position):
    data = fetch_embed(f"https://open.spotify.com/embed/track/{track_id}")
    entity = extract_entity(data)
    item = parse_track(entity, position)
    if item:
        return item
    track_list = entity.get("trackList") or []
    if track_list:
        return parse_track(track_list[0], position)
    raise RuntimeError(f"Could not parse Spotify track metadata for {track_id}")


def main():
    data = fetch_embed(EMBED_PLAYLIST)
    entity = extract_entity(data)
    token = extract_session_token(data)
    embedded = {}
    for pos, track in enumerate(entity.get("trackList") or [], start=1):
        item = parse_track(track, pos)
        if item:
            embedded[item["spotify_id"]] = item

    canonical_ids = []
    if token:
        r = session.get(
            SPCLIENT,
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
            timeout=40,
        )
        if r.status_code == 200:
            payload = r.json()
            for item in ((payload.get("contents") or {}).get("items") or []):
                uri = str(item.get("uri") or "")
                if uri.startswith("spotify:track:"):
                    canonical_ids.append(uri.rsplit(":", 1)[-1])
            declared = int(payload.get("length") or len(canonical_ids))
            print(f"spclient declared={declared} parsed={len(canonical_ids)}", flush=True)

    if not canonical_ids:
        canonical_ids = [
            x["spotify_id"] for x in sorted(embedded.values(), key=lambda x: x["position"])
        ]

    if len(canonical_ids) != EXPECTED_TRACKS:
        raise SystemExit(
            f"REFERENCE_IMPORT_BLOCKED: expected {EXPECTED_TRACKS} Spotify tracks, "
            f"but resolved {len(canonical_ids)}. Refusing to start generation with an incomplete reference set."
        )

    refs = [None] * len(canonical_ids)
    missing = []
    for idx, tid in enumerate(canonical_ids, start=1):
        item = embedded.get(tid)
        if item:
            item["position"] = idx
            refs[idx - 1] = item
        else:
            missing.append((idx, tid))

    if missing:
        print(f"Fetching metadata for {len(missing)} tracks beyond embed limit...", flush=True)
        with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
            jobs = {
                pool.submit(fetch_track_meta, tid, idx): (idx, tid)
                for idx, tid in missing
            }
            for future in concurrent.futures.as_completed(jobs):
                idx, tid = jobs[future]
                refs[idx - 1] = future.result()
                print(f"metadata {idx}/{EXPECTED_TRACKS}: {tid}", flush=True)

    if any(x is None for x in refs):
        missing_positions = [i + 1 for i, x in enumerate(refs) if x is None]
        raise SystemExit(f"REFERENCE_IMPORT_BLOCKED: unresolved positions: {missing_positions}")

    payload = {
        "version": 1,
        "playlist": {
            "spotify_id": PLAYLIST_ID,
            "spotify_url": PLAYLIST_URL,
            "name": str(entity.get("name") or entity.get("title") or "Alanzoka Twitch"),
            "owner": str(entity.get("subtitle") or ""),
            "expected_track_count": EXPECTED_TRACKS,
            "resolved_track_count": len(refs),
        },
        "reference_policy": {
            "copyright_check": "Every reference is treated as copyrighted unless explicit contrary evidence exists.",
            "source_audio": "never downloaded, stored, sampled, stemmed, remixed or supplied to the generator",
            "generation_input": "Spotify metadata plus non-expressive numerical audio features from ReccoBeats when available",
            "artist_and_title_in_prompt": False,
            "goal": "capture broad energy/tempo/mood/groove characteristics while creating a new composition from noise",
        },
        "imported_at": now(),
        "tracks": refs,
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"resolved": len(refs), "out": str(OUT)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
