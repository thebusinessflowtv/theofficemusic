#!/usr/bin/env python3
import base64
import datetime as dt
import json
import math
import os
import pathlib
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaFileUpload

REPO = os.environ["GITHUB_REPOSITORY"]
TOKEN = os.environ["GH_TOKEN"]
RUN_ID = int(os.environ["GITHUB_RUN_ID"])
RUN_URL = f"https://github.com/{REPO}/actions/runs/{RUN_ID}"
API = f"https://api.github.com/repos/{REPO}"
ROOT = pathlib.Path(".")
BUILD = ROOT / "build"

SESSION_ID = os.environ["SESSION_ID"]
TRACK_URLS_B64 = os.environ["TRACK_URLS_B64"]
DURATION_MINUTES = int(os.environ["DURATION_MINUTES"])
TITLE = os.environ["TITLE"]
DESCRIPTION = os.environ.get("DESCRIPTION", "")
THUMBNAIL_URL = os.environ.get("THUMBNAIL_URL", "")
LOOP_URL = os.environ.get("LOOP_URL", "")
RESUME_BROADCAST_ID = os.environ.get("RESUME_BROADCAST_ID", "").strip()
RESUME_STREAM_ID = os.environ.get("RESUME_STREAM_ID", "").strip()
SEGMENT_INDEX = int(os.environ["SEGMENT_INDEX"])
PRIVACY_STATUS = os.environ.get("PRIVACY_STATUS", "public")
SEGMENT_SECONDS_OVERRIDE = int(os.environ.get("SEGMENT_SECONDS_OVERRIDE", "0"))
PREWARM_LEAD_SECONDS = int(os.environ.get("PREWARM_LEAD_SECONDS", "600"))
MAX_SEGMENTS = int(os.environ.get("MAX_SEGMENTS", "0"))

YOUTUBE_CLIENT_ID = os.environ["YOUTUBE_CLIENT_ID"]
YOUTUBE_CLIENT_SECRET = os.environ["YOUTUBE_CLIENT_SECRET"]
YOUTUBE_REFRESH_TOKEN = os.environ["YOUTUBE_REFRESH_TOKEN"]
YOUTUBE_CHANNEL_ID = os.environ["YOUTUBE_CHANNEL_ID"]

encoder = None
feeder = None
encoder_log = None
feeder_log = None
took_over = False
duplicate = False

def utcnow():
    return dt.datetime.now(dt.timezone.utc)

def iso_now():
    return utcnow().isoformat()

def parse_ts(value):
    if not value:
        return None
    try:
        return dt.datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except Exception:
        return None

def api_request(method, path, body=None, timeout=30, allow_404=False):
    url = f"{API}/{path.lstrip('/')}"
    headers = {
        "Authorization": f"Bearer {TOKEN}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "MediaForge-Live-Segment",
    }
    data = None
    if body is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
            return json.loads(raw.decode()) if raw else {}
    except urllib.error.HTTPError as e:
        if allow_404 and e.code == 404:
            return None
        raw = e.read().decode("utf-8", "replace")
        raise RuntimeError(f"GitHub API {method} {path} failed: HTTP {e.code}: {raw[-1000:]}") from e

def github_get_json(path):
    obj = api_request("GET", f"contents/{path}", allow_404=True)
    if not obj:
        return None
    try:
        raw = base64.b64decode((obj.get("content") or "").replace("\n", ""))
        data = json.loads(raw.decode("utf-8"))
        return data
    except Exception:
        return None

def github_put_json(path, payload, message):
    existing = api_request("GET", f"contents/{path}", allow_404=True)
    body = {
        "message": message,
        "content": base64.b64encode(json.dumps(payload, ensure_ascii=False, indent=2).encode()).decode(),
        "branch": "main",
    }
    if existing and existing.get("sha"):
        body["sha"] = existing["sha"]
    return api_request("PUT", f"contents/{path}", body)

def github_run_state(run_id):
    try:
        obj = api_request("GET", f"actions/runs/{int(run_id)}")
        return obj.get("status"), obj.get("conclusion")
    except Exception:
        return None, None

def dispatch_workflow(inputs):
    return api_request(
        "POST",
        "actions/workflows/office-music-live.yml/dispatches",
        {"ref": "main", "inputs": {k: str(v) for k, v in inputs.items()}},
    )

def download(url, out, retries=5):
    if not url:
        return False
    for n in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "MediaForge-Live-Segment"})
            with urllib.request.urlopen(req, timeout=120) as r, open(out, "wb") as f:
                while True:
                    chunk = r.read(1024 * 1024)
                    if not chunk:
                        break
                    f.write(chunk)
            return pathlib.Path(out).stat().st_size > 0
        except Exception as e:
            print(f"download retry {n+1}/{retries}: {e}", flush=True)
            time.sleep(min(10, 2 + n * 2))
    return False

def run(cmd, check=True, capture=False):
    print("+", " ".join(map(str, cmd)), flush=True)
    return subprocess.run(cmd, check=check, text=True, capture_output=capture)

def validate():
    import re
    if not re.fullmatch(r"[0-9a-fA-F-]{36}", SESSION_ID):
        raise RuntimeError("Invalid session_id")
    if DURATION_MINUTES < 0 or DURATION_MINUTES > 10080:
        raise RuntimeError("duration_minutes must be 0..10080")
    if SEGMENT_INDEX < 1:
        raise RuntimeError("segment_index must be >=1")
    if PRIVACY_STATUS not in {"public", "unlisted", "private"}:
        raise RuntimeError("privacy_status must be public, unlisted, or private")
    if SEGMENT_SECONDS_OVERRIDE < 0 or SEGMENT_SECONDS_OVERRIDE > 18000:
        raise RuntimeError("segment_seconds_override must be 0..18000")
    if PREWARM_LEAD_SECONDS < 10 or PREWARM_LEAD_SECONDS > 1800:
        raise RuntimeError("prewarm_lead_seconds must be 10..1800")
    if MAX_SEGMENTS < 0 or MAX_SEGMENTS > 1000:
        raise RuntimeError("max_segments must be 0..1000")
    urls = json.loads(base64.b64decode(TRACK_URLS_B64).decode("utf-8"))
    if not isinstance(urls, list) or not urls:
        raise RuntimeError("No track URLs")
    BUILD.mkdir(parents=True, exist_ok=True)

def claim_segment():
    global duplicate
    path = f"control/live-segment-locks/{SESSION_ID}-{SEGMENT_INDEX}.json"
    existing = github_get_json(path)
    if existing:
        old_run = existing.get("run_id")
        if old_run and int(old_run) != RUN_ID:
            state, _ = github_run_state(old_run)
            if state in {"queued", "in_progress"}:
                print(f"Segment {SEGMENT_INDEX} already owned by active run {old_run}; duplicate exits.", flush=True)
                duplicate = True
                return False
    github_put_json(
        path,
        {
            "run_id": RUN_ID,
            "session_id": SESSION_ID,
            "segment_index": SEGMENT_INDEX,
            "claimed_at": iso_now(),
        },
        f"live: claim segment {SESSION_ID} {SEGMENT_INDEX}",
    )
    return True

def ffmpeg_valid(path, video_only=False):
    cmd = ["ffmpeg", "-hide_banner", "-v", "error", "-i", str(path)]
    if video_only:
        cmd += ["-map", "0:v:0"]
    cmd += ["-t", "1", "-f", "null", "-"]
    return subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0

def prepare_media():
    thumb_source = BUILD / "thumbnail-source"
    thumb = BUILD / "thumbnail.jpg"
    loop = BUILD / "loop.mp4"

    if THUMBNAIL_URL:
        download(THUMBNAIL_URL, thumb_source)

    if thumb_source.exists() and ffmpeg_valid(thumb_source):
        run([
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(thumb_source),
            "-frames:v", "1", "-vf",
            "scale=1280:720:force_original_aspect_ratio=decrease,pad=1280:720:(ow-iw)/2:(oh-ih)/2",
            str(thumb),
        ])
    else:
        raw = "".join((ROOT / "assets/bootstrap/thumb_compact.b64").read_text().split())
        thumb.write_bytes(base64.b64decode(raw))

    if not ffmpeg_valid(thumb):
        raise RuntimeError("Prepared thumbnail is invalid")

    have_loop = False
    visual_source = BUILD / "visual-source"
    if LOOP_URL and download(LOOP_URL, visual_source):
        if ffmpeg_valid(visual_source, video_only=True):
            mime = run(["file", "-b", "--mime-type", str(visual_source)], capture=True).stdout.strip()
            if mime.startswith("image/"):
                run([
                    "ffmpeg", "-hide_banner", "-loglevel", "warning", "-y", "-loop", "1",
                    "-i", str(visual_source), "-t", "12", "-vf",
                    "scale=1920:1080:force_original_aspect_ratio=decrease:flags=lanczos,pad=1920:1080:(ow-iw)/2:(oh-ih)/2,setsar=1,fps=60,format=yuv420p",
                    "-an", "-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
                    "-g", "120", "-keyint_min", "120", "-sc_threshold", "0", str(loop),
                ])
            else:
                loop.write_bytes(visual_source.read_bytes())
            have_loop = ffmpeg_valid(loop, video_only=True)

    if not have_loop:
        bootstrap = ROOT / "assets/bootstrap/loop_00.b64"
        if bootstrap.exists():
            try:
                loop.write_bytes(base64.b64decode("".join(bootstrap.read_text().split())))
                have_loop = ffmpeg_valid(loop, video_only=True)
            except Exception:
                have_loop = False

    if not have_loop:
        run([
            "ffmpeg", "-hide_banner", "-loglevel", "warning", "-y", "-loop", "1",
            "-i", str(thumb), "-t", "12", "-vf",
            "scale=1920:1080:force_original_aspect_ratio=decrease:flags=lanczos,pad=1920:1080:(ow-iw)/2:(oh-ih)/2,setsar=1,fps=60,format=yuv420p",
            "-an", "-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
            "-g", "120", "-keyint_min", "120", "-sc_threshold", "0", str(loop),
        ])

    if not ffmpeg_valid(loop, video_only=True):
        raise RuntimeError("Prepared loop is invalid")
    return thumb, loop

def youtube_client():
    creds = Credentials(
        token=None,
        refresh_token=YOUTUBE_REFRESH_TOKEN,
        token_uri="https://oauth2.googleapis.com/token",
        client_id=YOUTUBE_CLIENT_ID,
        client_secret=YOUTUBE_CLIENT_SECRET,
        scopes=["https://www.googleapis.com/auth/youtube"],
    )
    return build("youtube", "v3", credentials=creds, cache_discovery=False)

def create_or_resume_youtube(thumb):
    yt = youtube_client()
    ch = yt.channels().list(part="id,snippet", mine=True).execute()["items"][0]
    if ch["id"] != YOUTUBE_CHANNEL_ID:
        raise RuntimeError("YouTube channel mismatch")

    existing = github_get_json(f"control/live-results/{SESSION_ID}.json") or {}
    bid = RESUME_BROADCAST_ID
    sid = RESUME_STREAM_ID
    is_resume = bool(bid and sid)
    now = utcnow()

    if not is_resume:
        b = yt.liveBroadcasts().insert(
            part="snippet,status,contentDetails",
            body={
                "snippet": {
                    "title": TITLE[:100],
                    "description": DESCRIPTION[:5000],
                    "scheduledStartTime": (now + dt.timedelta(seconds=20)).isoformat(),
                },
                "status": {
                    "privacyStatus": PRIVACY_STATUS,
                    "selfDeclaredMadeForKids": False,
                },
                "contentDetails": {
                    "enableAutoStart": True,
                    "enableAutoStop": False,
                    "monitorStream": {"enableMonitorStream": False},
                },
            },
        ).execute()
        bid = b["id"]
        s = yt.liveStreams().insert(
            part="snippet,cdn,contentDetails",
            body={
                "snippet": {"title": f"Peter Lofi {SESSION_ID}"},
                "cdn": {"frameRate": "60fps", "ingestionType": "rtmp", "resolution": "1080p"},
                "contentDetails": {"isReusable": True},
            },
        ).execute()
        sid = s["id"]
        yt.liveBroadcasts().bind(part="id,contentDetails", id=bid, streamId=sid).execute()

    stream = yt.liveStreams().list(part="cdn,status", id=sid).execute()["items"][0]
    ing = stream["cdn"]["ingestionInfo"]
    rtmp = ing["ingestionAddress"].rstrip("/") + "/" + ing["streamName"]

    custom_thumb = existing.get("custom_thumbnail_applied", False)
    if not is_resume:
        try:
            yt.thumbnails().set(
                videoId=bid,
                media_body=MediaFileUpload(str(thumb), mimetype="image/jpeg", resumable=False),
            ).execute()
            custom_thumb = True
        except HttpError as e:
            custom_thumb = False
            if e.resp.status != 403:
                raise
            print("Custom thumbnail unavailable; continuing.", flush=True)

    session_started = existing.get("session_started_at") or existing.get("started_at") or now.isoformat()

    if is_resume:
        result = dict(existing)
        result.update({
            "platform": "youtube",
            "youtube_broadcast_id": bid,
            "youtube_stream_id": sid,
            "youtube_url": f"https://www.youtube.com/watch?v={bid}",
            "session_started_at": session_started,
            "prewarmed_segment_index": SEGMENT_INDEX,
            "prewarmed_run_id": RUN_ID,
            "prewarmed_at": now.isoformat(),
        })
    else:
        result = {
            "platform": "youtube",
            "status": "starting",
            "title": TITLE,
            "description": DESCRIPTION,
            "github_run_id": RUN_ID,
            "github_run_url": RUN_URL,
            "youtube_broadcast_id": bid,
            "youtube_stream_id": sid,
            "youtube_url": f"https://www.youtube.com/watch?v={bid}",
            "segment_index": SEGMENT_INDEX,
            "encoder_resolution": "1920x1080",
            "encoder_fps": 60,
            "encoder_bitrate_kbps": 8000,
            "custom_thumbnail_applied": custom_thumb,
            "privacy_status": PRIVACY_STATUS,
            "started_at": now.isoformat(),
            "session_started_at": session_started,
        }

    github_put_json(
        f"control/live-results/{SESSION_ID}.json",
        result,
        f"office-music: segment prepared {SESSION_ID} {SEGMENT_INDEX}",
    )
    return yt, bid, sid, rtmp, session_started

def mark_ready(bid, sid):
    github_put_json(
        f"control/live-ready/{SESSION_ID}-{SEGMENT_INDEX}.json",
        {
            "session_id": SESSION_ID,
            "segment_index": SEGMENT_INDEX,
            "run_id": RUN_ID,
            "broadcast_id": bid,
            "stream_id": sid,
            "ready": True,
            "prepared_at": iso_now(),
        },
        f"live: ready {SESSION_ID} {SEGMENT_INDEX}",
    )

def wait_takeover():
    print(f"Segment {SEGMENT_INDEX} prewarmed; waiting for takeover signal.", flush=True)
    deadline = time.time() + 3600
    while time.time() < deadline:
        state = github_get_json(f"control/live-takeover/{SESSION_ID}-{SEGMENT_INDEX}.json")
        if state and state.get("takeover") is True:
            print("Takeover signal received.", flush=True)
            return
        time.sleep(2)
    raise RuntimeError("Takeover signal not received before safety timeout")

def visual_offset(loop_path, session_started):
    pos = None
    if SEGMENT_INDEX > 1:
        h = github_get_json(f"control/live-handoffs/{SESSION_ID}.json")
        if h and int(h.get("from_segment_index") or 0) == SEGMENT_INDEX - 1:
            try:
                pos = float(h.get("visual_position_seconds") or 0.0)
            except Exception:
                pos = None
    if pos is None:
        epoch = parse_ts(session_started)
        pos = max(0.0, time.time() - epoch) if epoch else 0.0

    probe = run([
        "ffprobe", "-v", "error", "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1", str(loop_path),
    ], capture=True)
    try:
        dur = float(probe.stdout.strip().splitlines()[0])
    except Exception:
        dur = 0.0
    off = pos % dur if dur > 0 else 0.0
    print(f"visual position={pos:.3f}s loop={dur:.3f}s offset={off:.3f}s", flush=True)
    return off

def start_encoder(loop_path, rtmp, vis_offset):
    global encoder, feeder, encoder_log, feeder_log, took_over
    feeder_log = open(BUILD / "audio-feeder.log", "wb")
    encoder_log = open(BUILD / "ffmpeg.log", "wb")

    feeder = subprocess.Popen(
        [
            sys.executable, "scripts/mediaforge_live_audio_feeder.py",
            "--session-id", SESSION_ID,
            "--fallback-b64", TRACK_URLS_B64,
            "--segment-index", str(SEGMENT_INDEX),
        ],
        stdout=subprocess.PIPE,
        stderr=feeder_log,
    )

    video_args = ["-re", "-stream_loop", "-1"]
    if vis_offset > 0.05:
        video_args += ["-ss", f"{vis_offset:.3f}"]
    video_args += ["-i", str(loop_path)]

    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "warning",
        *video_args,
        "-thread_queue_size", "512",
        "-f", "s16le", "-ar", "48000", "-ac", "2", "-i", "pipe:0",
        "-map", "0:v:0", "-map", "1:a:0",
        "-vf", "scale=1920:1080:force_original_aspect_ratio=decrease:flags=lanczos,pad=1920:1080:(ow-iw)/2:(oh-ih)/2,setsar=1,fps=60,format=yuv420p",
        "-r", "60", "-s:v", "1920x1080", "-pix_fmt", "yuv420p",
        "-c:v", "libx264", "-preset", "veryfast", "-tune", "zerolatency",
        "-profile:v", "high", "-level:v", "4.2",
        "-b:v", "8000k", "-minrate", "8000k", "-maxrate", "8000k", "-bufsize", "16000k",
        "-g", "120", "-keyint_min", "120", "-sc_threshold", "0",
        "-x264-params", "nal-hrd=cbr:force-cfr=1",
        "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2",
        "-f", "flv", rtmp,
    ]
    encoder = subprocess.Popen(cmd, stdin=feeder.stdout, stdout=encoder_log, stderr=encoder_log)
    feeder.stdout.close()
    took_over = True

def tail(path, n=80):
    try:
        lines = pathlib.Path(path).read_text(errors="replace").splitlines()
        return "\n".join(lines[-n:])
    except Exception:
        return ""

def assert_processes():
    if encoder is None or encoder.poll() is not None:
        raise RuntimeError("Encoder stopped unexpectedly:\n" + tail(BUILD / "ffmpeg.log"))
    if feeder is None or feeder.poll() is not None:
        raise RuntimeError("Audio feeder stopped unexpectedly:\n" + tail(BUILD / "audio-feeder.log"))

def verify_live(yt, bid, sid):
    time.sleep(12)
    assert_processes()

    active = False
    for _ in range(48):
        items = yt.liveStreams().list(part="status", id=sid).execute().get("items") or []
        st = (items[0].get("status") or {}).get("streamStatus") if items else None
        print("streamStatus=", st, flush=True)
        if st == "active":
            active = True
            break
        time.sleep(5)
    if not active:
        raise RuntimeError("YouTube ingest did not become active")

    lifecycle = None
    for _ in range(24):
        items = yt.liveBroadcasts().list(part="status", id=bid).execute().get("items") or []
        lifecycle = (items[0].get("status") or {}).get("lifeCycleStatus") if items else None
        print("lifeCycleStatus=", lifecycle, flush=True)
        if lifecycle == "live":
            break
        try:
            yt.liveBroadcasts().transition(part="status", id=bid, broadcastStatus="live").execute()
        except Exception as e:
            print("transition live retry:", e, flush=True)
        time.sleep(5)

    items = yt.liveBroadcasts().list(part="status", id=bid).execute().get("items") or []
    lifecycle = (items[0].get("status") or {}).get("lifeCycleStatus") if items else lifecycle
    if lifecycle != "live":
        raise RuntimeError(f"Broadcast did not reach LIVE; lifecycle={lifecycle}")

    result = github_get_json(f"control/live-results/{SESSION_ID}.json") or {}
    now = iso_now()
    result.update({
        "platform": "youtube",
        "status": "live",
        "github_run_id": RUN_ID,
        "github_run_url": RUN_URL,
        "youtube_broadcast_id": bid,
        "youtube_stream_id": sid,
        "youtube_url": f"https://www.youtube.com/watch?v={bid}",
        "segment_index": SEGMENT_INDEX,
        "encoder_resolution": "1920x1080",
        "encoder_fps": 60,
        "encoder_bitrate_kbps": 8000,
        "current_segment_started_at": now,
        "last_verified_lifecycle": "live",
    })
    result.pop("prewarmed_segment_index", None)
    result.pop("prewarmed_run_id", None)
    result.pop("prewarmed_at", None)
    result.setdefault("live_at", now)
    github_put_json(
        f"control/live-results/{SESSION_ID}.json",
        result,
        f"office-music: live {SESSION_ID} segment {SEGMENT_INDEX}",
    )

def segment_seconds():
    if SEGMENT_SECONDS_OVERRIDE > 0:
        return SEGMENT_SECONDS_OVERRIDE
    if DURATION_MINUTES == 0:
        return 18000
    return min(DURATION_MINUTES, 300) * 60

def should_chain():
    if MAX_SEGMENTS > 0 and SEGMENT_INDEX >= MAX_SEGMENTS:
        return False
    if SEGMENT_SECONDS_OVERRIDE == 0 and 0 < DURATION_MINUTES <= 300:
        return False
    return True

def next_duration():
    if DURATION_MINUTES == 0:
        return 0
    if DURATION_MINUTES > 300:
        return DURATION_MINUTES - 300
    return DURATION_MINUTES

def next_inputs(bid, sid):
    return {
        "session_id": SESSION_ID,
        "track_urls_b64": TRACK_URLS_B64,
        "duration_minutes": next_duration(),
        "title": TITLE,
        "description": DESCRIPTION,
        "thumbnail_url": THUMBNAIL_URL,
        "loop_url": LOOP_URL,
        "resume_broadcast_id": bid,
        "resume_stream_id": sid,
        "segment_index": SEGMENT_INDEX + 1,
        "privacy_status": PRIVACY_STATUS,
        "segment_seconds_override": SEGMENT_SECONDS_OVERRIDE,
        "prewarm_lead_seconds": PREWARM_LEAD_SECONDS,
        "max_segments": MAX_SEGMENTS,
    }

def dispatch_next(bid, sid):
    for n in range(1, 6):
        try:
            dispatch_workflow(next_inputs(bid, sid))
            print(f"Successor segment {SEGMENT_INDEX+1} dispatched.", flush=True)
            return True
        except Exception as e:
            print(f"prewarm dispatch attempt {n} failed: {e}", flush=True)
            time.sleep(n * 5)
    return False

def successor_ready():
    state = github_get_json(f"control/live-ready/{SESSION_ID}-{SEGMENT_INDEX+1}.json")
    if not state or state.get("ready") is not True:
        return False
    if int(state.get("segment_index") or 0) != SEGMENT_INDEX + 1:
        return False
    run_id = state.get("run_id")
    if run_id:
        status, _ = github_run_state(run_id)
        return status in {"queued", "in_progress"}
    return True

def stop_requested():
    state = github_get_json(f"control/live-stop/{SESSION_ID}.json")
    return bool(state and state.get("stop") is True)

def capture_handoff(session_started):
    state = github_get_json(f"control/live-now-playing/{SESSION_ID}.json") or {}
    now = time.time()
    started = parse_ts(state.get("started_at"))
    if started is not None:
        position = max(0.0, now - started)
    else:
        position = float(state.get("resume_offset_seconds") or 0.0)
    sess_epoch = parse_ts(session_started)
    visual = max(0.0, now - sess_epoch) if sess_epoch else 0.0
    return {
        "session_id": SESSION_ID,
        "from_segment_index": SEGMENT_INDEX,
        "to_segment_index": SEGMENT_INDEX + 1,
        "track_id": state.get("track_id"),
        "title": state.get("title"),
        "url": state.get("url"),
        "track_started_at": state.get("started_at"),
        "position_seconds": round(position, 3),
        "visual_position_seconds": round(visual, 3),
        "recorded_at": iso_now(),
        "source_run_id": RUN_ID,
    }

def stop_processes():
    global encoder, feeder, encoder_log, feeder_log
    if encoder and encoder.poll() is None:
        try:
            encoder.send_signal(signal.SIGINT)
        except Exception:
            pass
    if feeder and feeder.poll() is None:
        try:
            feeder.terminate()
        except Exception:
            pass
    end = time.time() + 10
    while time.time() < end and encoder and encoder.poll() is None:
        time.sleep(0.25)
    if encoder and encoder.poll() is None:
        encoder.kill()
    if feeder and feeder.poll() is None:
        feeder.kill()
    if encoder_log:
        encoder_log.close()
    if feeder_log:
        feeder_log.close()

def run_segment(yt, bid, sid, session_started):
    chain = should_chain()
    seg_seconds = segment_seconds()
    lead = min(PREWARM_LEAD_SECONDS, max(10, seg_seconds // 2))
    start_epoch = int(time.time())
    deadline = start_epoch + seg_seconds
    prewarm_at = deadline - lead
    hard_deadline = deadline + 1200
    prewarm_sent = False
    last_redispatch = 0
    reason = "segment_complete"

    while True:
        assert_processes()
        now = int(time.time())

        if stop_requested():
            reason = "manual_stop"
            chain = False
            break

        if chain and not prewarm_sent and now >= prewarm_at:
            prewarm_sent = dispatch_next(bid, sid)
            last_redispatch = now

        if now >= deadline:
            if not chain:
                break
            if not prewarm_sent:
                prewarm_sent = dispatch_next(bid, sid)
                last_redispatch = now
            if successor_ready():
                print(f"Successor {SEGMENT_INDEX+1} is ready; performing handoff.", flush=True)
                break
            if now - last_redispatch >= 60:
                dispatch_next(bid, sid)
                last_redispatch = now
            if now >= hard_deadline:
                raise RuntimeError("Successor did not become ready before the 20-minute safety extension")
            print("Successor not ready; current encoder remains online.", flush=True)

        time.sleep(5)

    if chain and reason == "segment_complete":
        handoff = capture_handoff(session_started)
        stop_processes()
        github_put_json(
            f"control/live-handoffs/{SESSION_ID}.json",
            handoff,
            f"live: handoff {SESSION_ID} {SEGMENT_INDEX}",
        )
        github_put_json(
            f"control/live-takeover/{SESSION_ID}-{SEGMENT_INDEX+1}.json",
            {
                "session_id": SESSION_ID,
                "segment_index": SEGMENT_INDEX + 1,
                "from_segment_index": SEGMENT_INDEX,
                "takeover": True,
                "signaled_at": iso_now(),
            },
            f"live: takeover {SESSION_ID} {SEGMENT_INDEX+1}",
        )
        return True, reason

    stop_processes()
    return False, reason

def complete_broadcast(yt, bid):
    try:
        yt.liveBroadcasts().transition(part="status", id=bid, broadcastStatus="complete").execute()
    except Exception as e:
        print("complete transition:", e, flush=True)
    result = github_get_json(f"control/live-results/{SESSION_ID}.json") or {}
    result["status"] = "completed"
    result["completed_at"] = iso_now()
    github_put_json(
        f"control/live-results/{SESSION_ID}.json",
        result,
        f"office-music: live complete {SESSION_ID}",
    )

def record_failure(exc):
    payload = {
        "session_id": SESSION_ID,
        "segment_index": SEGMENT_INDEX,
        "run_id": RUN_ID,
        "run_url": RUN_URL,
        "error": str(exc),
        "failed_at": iso_now(),
        "took_over": took_over,
    }
    try:
        github_put_json(
            f"control/live-prewarm-failures/{SESSION_ID}-{SEGMENT_INDEX}.json",
            payload,
            f"live: segment failure {SESSION_ID} {SEGMENT_INDEX}",
        )
    except Exception as e:
        print("Could not write failure detail:", e, flush=True)

    if SEGMENT_INDEX > 1 and not took_over:
        return

    try:
        result = github_get_json(f"control/live-results/{SESSION_ID}.json") or {}
        result.update({
            "platform": "youtube",
            "status": "failed",
            "github_run_id": RUN_ID,
            "github_run_url": RUN_URL,
            "error_message": str(exc),
            "completed_at": iso_now(),
        })
        github_put_json(
            f"control/live-results/{SESSION_ID}.json",
            result,
            f"office-music: live failed {SESSION_ID}",
        )
    except Exception as e:
        print("Could not update live failure status:", e, flush=True)

def main():
    validate()
    if not claim_segment():
        return

    thumb, loop = prepare_media()
    yt, bid, sid, rtmp, session_started = create_or_resume_youtube(thumb)

    if SEGMENT_INDEX > 1:
        mark_ready(bid, sid)
        wait_takeover()

    vis = visual_offset(loop, session_started)
    start_encoder(loop, rtmp, vis)
    verify_live(yt, bid, sid)

    chained, _ = run_segment(yt, bid, sid, session_started)
    if not chained:
        complete_broadcast(yt, bid)

if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"FATAL: {exc}", file=sys.stderr, flush=True)
        try:
            stop_processes()
        except Exception:
            pass
        try:
            record_failure(exc)
        except Exception as failure_exc:
            print(f"failure recorder also failed: {failure_exc}", file=sys.stderr, flush=True)
        raise
