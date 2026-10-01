#!/usr/bin/env python3
import argparse, base64, json, os, subprocess, sys, time, urllib.request, urllib.error

RAW_BASE = "https://raw.githubusercontent.com/thebusinessflowtv/theofficemusic/main"
API_BASE = "https://api.github.com/repos/thebusinessflowtv/theofficemusic/contents"

def fetch_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": "MediaForge-Live-Audio"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)

def get_playlist(session_id, fallback):
    url = f"{RAW_BASE}/control/live-playlists/{session_id}.json?ts={int(time.time())}"
    try:
        data = fetch_json(url)
        tracks = data.get("tracks") or []
        clean = []
        for t in tracks:
            if isinstance(t, str):
                clean.append({"id": t, "title": t, "url": t})
            elif isinstance(t, dict) and t.get("url"):
                clean.append({"id": str(t.get("id") or t["url"]), "title": str(t.get("title") or t.get("id") or "Faixa"), "url": str(t["url"])})
        if clean:
            return clean
    except Exception as e:
        print(f"playlist refresh warning: {e}", file=sys.stderr, flush=True)
    return fallback

def github_upsert(path, payload):
    token = os.environ.get("GH_TOKEN", "").strip()
    repo = os.environ.get("GITHUB_REPOSITORY", "thebusinessflowtv/theofficemusic")
    if not token:
        return
    api = f"https://api.github.com/repos/{repo}/contents/{path}"
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "MediaForge-Live-Audio",
        "Content-Type": "application/json",
    }
    sha = None
    try:
        req = urllib.request.Request(api, headers=headers)
        with urllib.request.urlopen(req, timeout=20) as r:
            sha = json.load(r).get("sha")
    except Exception:
        pass
    body = {
        "message": f"mediaforge: now playing {payload.get('session_id','')}",
        "content": base64.b64encode(json.dumps(payload, ensure_ascii=False, indent=2).encode()).decode(),
        "branch": "main",
    }
    if sha:
        body["sha"] = sha
    req = urllib.request.Request(api, data=json.dumps(body).encode(), headers=headers, method="PUT")
    try:
        with urllib.request.urlopen(req, timeout=30):
            pass
    except Exception as e:
        print(f"now-playing publish warning: {e}", file=sys.stderr, flush=True)

def play_track(track):
    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-re",
        "-i", track["url"], "-vn", "-ac", "2", "-ar", "48000",
        "-f", "s16le", "pipe:1"
    ]
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        while True:
            chunk = p.stdout.read(262144)
            if not chunk:
                break
            sys.stdout.buffer.write(chunk)
            sys.stdout.buffer.flush()
    except BrokenPipeError:
        p.terminate()
        raise SystemExit(0)
    finally:
        try:
            p.wait(timeout=10)
        except subprocess.TimeoutExpired:
            p.kill()
    if p.returncode not in (0, None):
        err = p.stderr.read().decode("utf-8", "replace")[-2000:]
        print(f"track ffmpeg failed: {track['url']}\n{err}", file=sys.stderr, flush=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--session-id", required=True)
    ap.add_argument("--fallback-b64", required=True)
    args = ap.parse_args()
    urls = json.loads(base64.b64decode(args.fallback_b64).decode("utf-8"))
    fallback = [{"id": str(i), "title": f"Faixa {i}", "url": str(u)} for i, u in enumerate(urls, 1)]
    if not fallback:
        raise SystemExit("No fallback tracks")
    cursor = 0
    while True:
        playlist = get_playlist(args.session_id, fallback)
        if not playlist:
            time.sleep(2)
            continue
        if cursor >= len(playlist):
            cursor = 0
        track = playlist[cursor]
        cursor += 1
        payload = {
            "session_id": args.session_id,
            "track_id": track.get("id"),
            "title": track.get("title"),
            "url": track.get("url"),
            "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "playlist_size": len(playlist),
        }
        github_upsert(f"control/live-now-playing/{args.session_id}.json", payload)
        play_track(track)

if __name__ == "__main__":
    main()
