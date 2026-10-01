#!/usr/bin/env python3
import argparse, base64, json, os, pathlib, subprocess, sys, time, urllib.request
from datetime import datetime, timezone

RAW_BASE = "https://raw.githubusercontent.com/thebusinessflowtv/theofficemusic/main"
PCM_CHUNK = 32768  # ~171 ms of stereo 48 kHz s16le; small enough for smooth continuous delivery.
LOCAL_NOW_PLAYING = pathlib.Path("build/now-playing.json")


def fetch_json(url, timeout=30):
    req = urllib.request.Request(url, headers={"User-Agent": "MediaForge-Live-Audio"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def raw_json(path):
    return fetch_json(f"{RAW_BASE}/{path}?ts={int(time.time()*1000)}")


def get_playlist(session_id, fallback):
    try:
        data = raw_json(f"control/live-playlists/{session_id}.json")
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


def publish_now_playing(path, payload):
    """Keep precise now-playing state local to the runner.

    Previous versions performed a GitHub REST GET+PUT every ~2 minutes for every
    live stream. Across multiple 24/7 lives this consumed the installation API
    quota and could indirectly kill the RTMP job during a handoff. The encoder
    no longer depends on those REST writes. Repository publication is opt-in and
    disabled by default; handoff code reads this local file instead.
    """
    try:
        LOCAL_NOW_PLAYING.parent.mkdir(parents=True, exist_ok=True)
        tmp = LOCAL_NOW_PLAYING.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        tmp.replace(LOCAL_NOW_PLAYING)
    except Exception as e:
        print(f"local now-playing warning: {e}", file=sys.stderr, flush=True)

    if os.environ.get("MEDIAFORGE_PUBLISH_NOW_PLAYING", "0") != "1":
        return

    # Optional legacy publication only for diagnostics; never used by production continuity.
    token = os.environ.get("GH_TOKEN", "").strip()
    repo = os.environ.get("GITHUB_REPOSITORY", "thebusinessflowtv/theofficemusic")
    if not token:
        return
    api = f"https://api.github.com/repos/{repo}/contents/{path}"
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "MediaForge-Live-Audio-Diagnostics", "Content-Type": "application/json"}
    try:
        sha = None
        try:
            req = urllib.request.Request(api, headers=headers)
            with urllib.request.urlopen(req, timeout=10) as r:
                sha = json.load(r).get("sha")
        except Exception:
            pass
        body = {"message": f"mediaforge: now playing {payload.get('session_id','')}", "content": base64.b64encode(json.dumps(payload, ensure_ascii=False, indent=2).encode()).decode(), "branch": "main"}
        if sha:
            body["sha"] = sha
        req = urllib.request.Request(api, data=json.dumps(body).encode(), headers=headers, method="PUT")
        with urllib.request.urlopen(req, timeout=15):
            pass
    except Exception as e:
        print(f"optional now-playing publish warning: {e}", file=sys.stderr, flush=True)


def parse_ts(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except Exception:
        return None


def iso_from_epoch(ts):
    return datetime.fromtimestamp(ts, timezone.utc).isoformat().replace("+00:00", "Z")


def find_track_index(playlist, state):
    su, si = str(state.get("url") or ""), str(state.get("track_id") or "")
    for i, t in enumerate(playlist):
        if su and str(t.get("url") or "") == su:
            return i
    for i, t in enumerate(playlist):
        if si and str(t.get("id") or "") == si:
            return i
    return None


def load_resume_state(session_id, segment_index):
    if segment_index <= 1:
        return None
    expected_prev = segment_index - 1

    for _ in range(6):
        try:
            state = raw_json(f"control/live-takeover/{session_id}-{segment_index}.json")
            if state.get("takeover") is True and int(state.get("from_segment_index") or 0) == expected_prev and state.get("url"):
                state["_source"] = "takeover"
                return state
        except Exception:
            pass
        time.sleep(0.35)

    for _ in range(6):
        try:
            state = raw_json(f"control/live-handoffs/{session_id}.json")
            if int(state.get("from_segment_index") or 0) == expected_prev and state.get("url"):
                state["_source"] = "handoff"
                return state
        except Exception:
            pass
        time.sleep(0.35)

    # Legacy fallback only. New handoffs always persist takeover before cutover.
    try:
        state = raw_json(f"control/live-now-playing/{session_id}.json")
        if state.get("url"):
            started = parse_ts(state.get("started_at"))
            if started is not None:
                state["position_seconds"] = max(0.0, time.time() - started)
            state["_source"] = "now-playing-legacy"
            return state
    except Exception as e:
        print(f"resume-state warning: {e}", file=sys.stderr, flush=True)
    return None


def _write_all(fd, chunk):
    view = memoryview(chunk)
    while view:
        written = os.write(fd, view)
        view = view[written:]


def play_track(track, start_offset=0.0):
    """Decode ahead of realtime and let the RTMP encoder provide pacing."""
    start_offset = max(0.0, float(start_offset or 0.0))
    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin",
        "-reconnect", "1", "-reconnect_streamed", "1", "-reconnect_delay_max", "5",
    ]
    if start_offset > 0.05:
        cmd += ["-ss", f"{start_offset:.3f}"]
    cmd += [
        "-i", track["url"],
        "-vn", "-sn", "-dn",
        "-af", "aresample=48000:async=1:first_pts=0",
        "-ac", "2", "-ar", "48000", "-c:a", "pcm_s16le", "-f", "s16le", "pipe:1",
    ]
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0)
    sent = 0
    try:
        out_fd = sys.stdout.fileno()
        src_fd = p.stdout.fileno()
        while True:
            chunk = os.read(src_fd, PCM_CHUNK)
            if not chunk:
                break
            sent += len(chunk)
            _write_all(out_fd, chunk)
    except BrokenPipeError:
        p.terminate()
        raise SystemExit(0)
    finally:
        try:
            p.wait(timeout=10)
        except subprocess.TimeoutExpired:
            p.kill()
            p.wait()
    if p.returncode not in (0, None):
        err = p.stderr.read().decode("utf-8", "replace")[-2000:]
        print(f"track ffmpeg failed: {track['url']}\n{err}", file=sys.stderr, flush=True)
    return sent > 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--session-id", required=True)
    ap.add_argument("--fallback-b64", required=True)
    ap.add_argument("--segment-index", type=int, default=1)
    args = ap.parse_args()

    urls = json.loads(base64.b64decode(args.fallback_b64).decode("utf-8"))
    fallback = [{"id": str(i), "title": f"Faixa {i}", "url": str(u)} for i, u in enumerate(urls, 1)]
    if not fallback:
        raise SystemExit("No fallback tracks")

    playlist = get_playlist(args.session_id, fallback)
    resume = load_resume_state(args.session_id, args.segment_index)
    cursor = 0
    pending_track, pending_offset, resume_source = None, 0.0, None

    if resume:
        idx = find_track_index(playlist, resume)
        pending_track = {"id": str(resume.get("track_id") or (playlist[idx]["id"] if idx is not None else resume.get("url"))), "title": str(resume.get("title") or (playlist[idx]["title"] if idx is not None else "Faixa")), "url": str(resume.get("url") or (playlist[idx]["url"] if idx is not None else ""))}
        pending_offset = max(0.0, float(resume.get("position_seconds") or 0.0))
        resume_source = resume.get("_source")
        if idx is not None:
            cursor = idx + 1
        print(f"Resuming segment {args.segment_index} from {resume_source}: {pending_track['title']} @ {pending_offset:.3f}s", file=sys.stderr, flush=True)

    while True:
        playlist = get_playlist(args.session_id, fallback)
        if not playlist:
            time.sleep(2)
            continue
        offset, source = 0.0, "playlist"
        if pending_track and pending_track.get("url"):
            track, offset, source = pending_track, pending_offset, f"resume:{resume_source or 'state'}"
            pending_track, pending_offset = None, 0.0
        else:
            if cursor >= len(playlist):
                cursor = 0
            track = playlist[cursor]
            cursor += 1

        virtual_started = time.time() - offset
        payload = {"session_id": args.session_id, "segment_index": args.segment_index, "track_id": track.get("id"), "title": track.get("title"), "url": track.get("url"), "started_at": iso_from_epoch(virtual_started), "resumed_at": iso_from_epoch(time.time()) if offset > 0 else None, "resume_offset_seconds": round(offset, 3), "resume_source": source, "playlist_size": len(playlist)}
        publish_now_playing(f"control/live-now-playing/{args.session_id}.json", payload)
        played = play_track(track, offset)
        if not played and offset > 0:
            print("Resume offset produced no audio; advancing to next track.", file=sys.stderr, flush=True)


if __name__ == "__main__":
    main()
