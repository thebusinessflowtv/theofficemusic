#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import mimetypes
import os
import pathlib
import subprocess
import tempfile
import urllib.parse
import urllib.request

CONFIG_PATHS = (
    "control/music-library.json",
    "control/mediaforge-catalog.json",
)
GITHUB_RELEASE_PREFIX = "https://github.com/thebusinessflowtv/theofficemusic/releases/download/"


def api_json(api: str, token: str, path: str, method: str = "GET", body=None, timeout: int = 120):
    data = None
    headers = {
        "User-Agent": "MediaForge-OVH-Library-Migrator",
        "x-ovh-agent-token": token,
    }
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers["content-type"] = "application/json"
    req = urllib.request.Request(api.rstrip("/") + path, data=data, method=method, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def collect_urls(value, titles: dict[str, str], inherited_title: str = ""):
    if isinstance(value, dict):
        title = str(value.get("title") or value.get("name") or inherited_title or "").strip()
        for key, item in value.items():
            if isinstance(item, str) and item.startswith(GITHUB_RELEASE_PREFIX):
                titles.setdefault(item, title)
            else:
                collect_urls(item, titles, title)
    elif isinstance(value, list):
        for item in value:
            collect_urls(item, titles, inherited_title)
    elif isinstance(value, str) and value.startswith(GITHUB_RELEASE_PREFIX):
        titles.setdefault(value, inherited_title)


def atomic_json(path: pathlib.Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.chmod(temp, 0o600)
    temp.replace(path)


def download(url: str, target: pathlib.Path) -> None:
    subprocess.run([
        "curl", "-fL", "--retry", "6", "--retry-all-errors", "--retry-delay", "2",
        "--connect-timeout", "30", "--max-time", "1800",
        "-o", str(target), url,
    ], check=True)


def upload(api: str, token: str, path: pathlib.Path, title: str):
    mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    query = urllib.parse.urlencode({
        "name": path.name,
        "title": title or path.stem,
        "asset_type": "audio",
    })
    cmd = [
        "curl", "-fsS", "--retry", "4", "--retry-all-errors", "--retry-delay", "2",
        "-X", "PUT", f"{api.rstrip('/')}/api/ovh/agent/assets?{query}",
        "-H", f"x-ovh-agent-token: {token}",
        "-H", f"content-type: {mime}",
        "-H", f"content-length: {path.stat().st_size}",
        "--data-binary", f"@{path}",
    ]
    raw = subprocess.check_output(cmd, text=True)
    data = json.loads(raw)
    asset = data.get("asset") or {}
    if not asset.get("runtime_url") or not asset.get("public_url"):
        raise RuntimeError(f"OVH asset upload did not return URLs for {path.name}")
    return asset


def replace_urls(value, mapping: dict[str, dict], key_hint: str = ""):
    if isinstance(value, dict):
        out = {}
        for key, item in value.items():
            out[key] = replace_urls(item, mapping, str(key))
        return out
    if isinstance(value, list):
        return [replace_urls(item, mapping, key_hint) for item in value]
    if isinstance(value, str) and value in mapping:
        row = mapping[value]
        if key_hint in {"public_url", "download_url", "master_audio_url"}:
            return row["public_url"]
        return row["runtime_url"]
    return value


def count_github(value) -> int:
    if isinstance(value, dict):
        return sum(count_github(x) for x in value.values())
    if isinstance(value, list):
        return sum(count_github(x) for x in value)
    if isinstance(value, str) and value.startswith(GITHUB_RELEASE_PREFIX):
        return 1
    return 0


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--api", default="http://127.0.0.1:8790")
    ap.add_argument("--agent-token", required=True)
    ap.add_argument("--map", default="/opt/mediaforge-control/music-url-map.json")
    ap.add_argument("--cache-dir", default="/opt/mediaforge-control/library-migration-cache")
    ap.add_argument("--stream-state", default="/home/ubuntu/theofficemusic/ovh-streaming/state")
    ap.add_argument("--stations-dir", default="/home/ubuntu/theofficemusic/ovh-streaming/stations")
    ap.add_argument("--audio-cache", default="/home/ubuntu/theofficemusic/ovh-streaming/state/audio-cache")
    args = ap.parse_args()

    api = args.api.rstrip("/")
    token = args.agent_token
    map_path = pathlib.Path(args.map)
    cache = pathlib.Path(args.cache_dir)
    cache.mkdir(parents=True, exist_ok=True)
    os.chmod(cache, 0o700)

    configs = {}
    titles: dict[str, str] = {}
    for path in CONFIG_PATHS:
        row = api_json(api, token, "/api/ovh/agent/runtime-config?path=" + urllib.parse.quote(path, safe=""))
        payload = row.get("payload")
        if payload is None:
            raise RuntimeError(f"Local runtime config missing: {path}")
        configs[path] = payload
        collect_urls(payload, titles)

    urls = sorted(titles)
    mapping = {}
    if map_path.is_file():
        try:
            mapping = json.loads(map_path.read_text(encoding="utf-8"))
        except Exception:
            mapping = {}

    print(f"MediaForge library localization: {len(urls)} unique GitHub audio URL(s).", flush=True)
    print(f"Already migrated: {sum(1 for u in urls if u in mapping)}", flush=True)

    for index, url in enumerate(urls, 1):
        if url in mapping and mapping[url].get("runtime_url") and mapping[url].get("public_url"):
            continue
        parsed = urllib.parse.urlparse(url)
        name = urllib.parse.unquote(pathlib.PurePosixPath(parsed.path).name) or f"track-{index:03d}.bin"
        safe = "".join(c if c.isalnum() or c in "._-" else "-" for c in name)[-180:] or f"track-{index:03d}.bin"
        target = cache / f"{index:04d}-{safe}"
        title = titles.get(url) or pathlib.Path(name).stem
        print(f"[{index}/{len(urls)}] {title} -> OVH", flush=True)
        try:
            download(url, target)
            if target.stat().st_size <= 0:
                raise RuntimeError(f"Downloaded empty file: {name}")
            asset = upload(api, token, target, title)
            mapping[url] = {
                "asset_id": asset["id"],
                "runtime_url": asset["runtime_url"],
                "public_url": asset["public_url"],
                "size_bytes": int(asset.get("size_bytes") or target.stat().st_size),
                "source_url": url,
            }
            atomic_json(map_path, mapping)
        finally:
            target.unlink(missing_ok=True)

    # Preserve any already-warmed audio cache without duplicating gigabytes.
    # AudioEngine keys cached files by sha256(URL)[:32] + source suffix.
    cache_root = pathlib.Path(args.audio_cache)
    cache_links = 0
    if cache_root.is_dir():
        for old_url, row in mapping.items():
            new_url = str(row.get("runtime_url") or "")
            if not new_url:
                continue
            old_suffix = pathlib.PurePosixPath(urllib.parse.urlparse(old_url).path).suffix.lower()
            new_suffix = pathlib.PurePosixPath(urllib.parse.urlparse(new_url).path).suffix.lower()
            suffix = new_suffix or old_suffix or ".media"
            if len(suffix) > 10:
                suffix = ".media"
            old_name = hashlib.sha256(old_url.encode("utf-8")).hexdigest()[:32] + (old_suffix if old_suffix and len(old_suffix) <= 10 else ".media")
            new_name = hashlib.sha256(new_url.encode("utf-8")).hexdigest()[:32] + suffix
            old_path = cache_root / old_name
            new_path = cache_root / new_name
            if old_path.is_file() and old_path.stat().st_size >= 4096 and not new_path.exists():
                try:
                    os.link(old_path, new_path)
                    cache_links += 1
                except OSError:
                    pass

    for path, payload in configs.items():
        localized = replace_urls(payload, mapping)
        remaining = count_github(localized)
        if remaining:
            raise RuntimeError(f"{path} still contains {remaining} GitHub Release audio URL(s)")
        api_json(api, token, "/api/ovh/agent/runtime-config", "POST", {"path": path, "payload": localized}, timeout=180)
        print(f"{path}: localized and stored on OVH.", flush=True)

    # Rewrite the active OVH runtime playlists in place. AudioEngine watches
    # playlist.json mtime, so this changes the source for the next track without
    # restarting the persistent RTMP encoder.
    localized_files = 0
    local_roots = [pathlib.Path(args.stream_state), pathlib.Path(args.stations_dir)]
    for root in local_roots:
        if not root.exists():
            continue
        candidates = list(root.glob("*/playlist.json")) if root.name == "state" else list(root.glob("*.json"))
        for path in candidates:
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                continue
            localized = replace_urls(payload, mapping)
            if count_github(localized):
                raise RuntimeError(f"{path} still contains GitHub Release audio URLs")
            if localized != payload:
                temp = path.with_suffix(path.suffix + ".tmp")
                temp.write_text(json.dumps(localized, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
                temp.replace(path)
                localized_files += 1
                print(f"{path}: active runtime playlist localized without encoder restart.", flush=True)

    total_bytes = sum(int(mapping[u].get("size_bytes") or 0) for u in urls)
    print(json.dumps({
        "status": "MEDIAFORGE_LIBRARY_OVH_LOCAL",
        "unique_audio_assets": len(urls),
        "bytes_localized": total_bytes,
        "gib_localized": round(total_bytes / (1024**3), 3),
        "github_release_urls_remaining": 0,
        "active_playlist_files_rewritten": localized_files,
        "audio_cache_hardlinks_created": cache_links,
        "map_path": str(map_path),
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
