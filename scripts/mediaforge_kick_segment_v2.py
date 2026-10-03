#!/usr/bin/env python3
import base64
import datetime as dt
import json
import os
import pathlib
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request

REPO = os.environ["GITHUB_REPOSITORY"]
TOKEN = os.environ["GH_TOKEN"]
RUN_ID = int(os.environ["GITHUB_RUN_ID"])
RUN_URL = f"https://github.com/{REPO}/actions/runs/{RUN_ID}"
API = f"https://api.github.com/repos/{REPO}"
SESSION_ID = os.environ["SESSION_ID"]
TRACK_URLS_B64 = os.environ["TRACK_URLS_B64"]
DURATION_MINUTES = int(os.environ["DURATION_MINUTES"])
TITLE = os.environ["TITLE"]
DESCRIPTION = os.environ.get("DESCRIPTION", "")
THUMBNAIL_URL = os.environ.get("THUMBNAIL_URL", "")
LOOP_URL = os.environ.get("LOOP_URL", "")
SEGMENT_INDEX = int(os.environ["SEGMENT_INDEX"])
KICK_STREAM_KEY = os.environ["KICK_STREAM_KEY"].strip()
KICK_STREAM_URL = os.environ.get("KICK_STREAM_URL", "").strip()
KICK_RTMP_URL = os.environ.get("KICK_RTMP_URL", "").strip()
KICK_ACCESS_TOKEN = os.environ.get("KICK_ACCESS_TOKEN", "").strip()
KICK_CHANNEL_SLUG = os.environ.get("KICK_CHANNEL_SLUG", "peterlofi").strip() or "peterlofi"
BUILD = pathlib.Path("build")
encoder = feeder = encoder_log = feeder_log = None

def iso_now():
    return dt.datetime.now(dt.timezone.utc).isoformat()

def parse_ts(value):
    if not value:
        return None
    try:
        return dt.datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except Exception:
        return None

def api_request(method, path, body=None, timeout=30, allow_404=False):
    url = f"{API}/{path.lstrip('/')}"
    headers = {"Authorization": f"Bearer {TOKEN}", "Accept": "application/vnd.github+json",
               "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "MediaForge-Kick-V2"}
    data = None
    if body is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(body).encode()
    last = None
    for attempt in range(1, 9):
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                raw = r.read()
                return json.loads(raw.decode()) if raw else {}
        except urllib.error.HTTPError as exc:
            if allow_404 and exc.code == 404:
                return None
            if exc.code == 429 or exc.code >= 500:
                last = exc
                time.sleep(min(5, attempt))
                continue
            raw = exc.read().decode("utf-8", "replace")
            raise RuntimeError(f"GitHub API {method} {path} HTTP {exc.code}: {raw[-1000:]}")
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as exc:
            last = exc
            print(f"GitHub transport retry {attempt}/8: {exc}", flush=True)
            time.sleep(min(5, attempt))
    raise RuntimeError(f"GitHub API {method} {path} failed after retries: {last}")

def github_get_json(path):
    obj = api_request("GET", f"contents/{path}", allow_404=True)
    if not obj:
        return None
    try:
        return json.loads(base64.b64decode((obj.get("content") or "").replace("\n", "")).decode())
    except Exception:
        return None

def github_put_json(path, payload, message):
    last = None
    for attempt in range(1, 9):
        try:
            existing = api_request("GET", f"contents/{path}", allow_404=True)
            body = {"message": message,
                    "content": base64.b64encode(json.dumps(payload, ensure_ascii=False, indent=2).encode()).decode(),
                    "branch": "main"}
            if existing and existing.get("sha"):
                body["sha"] = existing["sha"]
            return api_request("PUT", f"contents/{path}", body)
        except Exception as exc:
            last = exc
            print(f"state write retry {attempt}/8 {path}: {exc}", flush=True)
            time.sleep(min(4, attempt))
    raise RuntimeError(f"Could not write {path}: {last}")

def dispatch_next():
    remain = 0 if DURATION_MINUTES == 0 else max(1, DURATION_MINUTES - 300)
    inputs = {"session_id": SESSION_ID, "track_urls_b64": TRACK_URLS_B64,
              "duration_minutes": str(remain), "title": TITLE, "description": DESCRIPTION,
              "thumbnail_url": THUMBNAIL_URL, "loop_url": LOOP_URL,
              "segment_index": str(SEGMENT_INDEX + 1)}
    try:
        api_request("POST", "actions/workflows/peter-lofi-kick-live.yml/dispatches",
                    {"ref": "main", "inputs": inputs})
        print(f"Successor segment {SEGMENT_INDEX + 1} dispatched.", flush=True)
        return True
    except Exception as exc:
        print(f"successor dispatch warning: {exc}", flush=True)
        return False

def download(url, out):
    if not url:
        return False
    for attempt in range(1, 6):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "MediaForge-Kick-V2"})
            with urllib.request.urlopen(req, timeout=180) as r, open(out, "wb") as f:
                while True:
                    chunk = r.read(1048576)
                    if not chunk:
                        break
                    f.write(chunk)
            return pathlib.Path(out).stat().st_size > 0
        except Exception as exc:
            print(f"download retry {attempt}/5: {exc}", flush=True)
            time.sleep(min(8, attempt * 2))
    return False

def ffmpeg_valid(path, video_only=False):
    cmd = ["ffmpeg", "-hide_banner", "-v", "error", "-i", str(path)]
    if video_only:
        cmd += ["-map", "0:v:0"]
    cmd += ["-t", "1", "-f", "null", "-"]
    return subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0

def prepare_media():
    BUILD.mkdir(parents=True, exist_ok=True)
    thumb_source, thumb, loop, source = [BUILD / x for x in ("thumbnail-source","thumbnail.jpg","loop.mp4","visual-source")]
    if THUMBNAIL_URL:
        download(THUMBNAIL_URL, thumb_source)
    if thumb_source.exists() and ffmpeg_valid(thumb_source):
        subprocess.run(["ffmpeg","-hide_banner","-loglevel","error","-y","-i",str(thumb_source),
                        "-frames:v","1","-vf","scale=1920:1080:force_original_aspect_ratio=decrease,pad=1920:1080:(ow-iw)/2:(oh-ih)/2",str(thumb)], check=True)
    else:
        raw = "".join(pathlib.Path("assets/bootstrap/thumb_compact.b64").read_text().split())
        thumb.write_bytes(base64.b64decode(raw))
    have_loop = False
    if LOOP_URL and download(LOOP_URL, source) and ffmpeg_valid(source, True):
        mime = subprocess.run(["file","-b","--mime-type",str(source)], text=True, capture_output=True, check=True).stdout.strip()
        if mime.startswith("image/"):
            subprocess.run(["ffmpeg","-hide_banner","-loglevel","warning","-y","-loop","1","-i",str(source),"-t","12",
                            "-vf","scale=1920:1080:force_original_aspect_ratio=decrease:flags=lanczos,pad=1920:1080:(ow-iw)/2:(oh-ih)/2,setsar=1,fps=60,format=yuv420p",
                            "-an","-c:v","libx264","-preset","veryfast","-crf","18","-g","120","-keyint_min","120","-sc_threshold","0",str(loop)], check=True)
        else:
            loop.write_bytes(source.read_bytes())
        have_loop = ffmpeg_valid(loop, True)
    if not have_loop:
        subprocess.run(["ffmpeg","-hide_banner","-loglevel","warning","-y","-loop","1","-i",str(thumb),"-t","12",
                        "-vf","scale=1920:1080:force_original_aspect_ratio=decrease:flags=lanczos,pad=1920:1080:(ow-iw)/2:(oh-ih)/2,setsar=1,fps=60,format=yuv420p",
                        "-an","-c:v","libx264","-preset","veryfast","-crf","18","-g","120","-keyint_min","120","-sc_threshold","0",str(loop)], check=True)
    if not ffmpeg_valid(loop, True):
        raise RuntimeError("Prepared Kick loop invalid")
    return loop

def kick_state():
    if KICK_ACCESS_TOKEN:
        try:
            req = urllib.request.Request("https://api.kick.com/public/v1/channels",
                headers={"Authorization":f"Bearer {KICK_ACCESS_TOKEN}","User-Agent":"MediaForge-Kick-V2"})
            with urllib.request.urlopen(req, timeout=10) as r:
                data = json.load(r)
            return "online" if ((data.get("data") or [{}])[0].get("stream") or {}).get("is_live") is True else "offline"
        except Exception:
            pass
    try:
        req = urllib.request.Request(f"https://kick.com/api/v2/channels/{KICK_CHANNEL_SLUG}",
                                     headers={"User-Agent":"Mozilla/5.0 MediaForge/2.0"})
        with urllib.request.urlopen(req, timeout=10) as r:
            data = json.load(r)
        return "online" if data.get("livestream") is not None else "offline"
    except Exception:
        return "unavailable"

def apply_title():
    if not KICK_ACCESS_TOKEN:
        return
    try:
        req = urllib.request.Request("https://api.kick.com/public/v1/channels",
            data=json.dumps({"stream_title":TITLE}).encode(),
            headers={"Authorization":f"Bearer {KICK_ACCESS_TOKEN}","Content-Type":"application/json","User-Agent":"MediaForge-Kick-V2"},
            method="PATCH")
        with urllib.request.urlopen(req, timeout=20):
            pass
    except Exception as exc:
        print(f"Kick title warning: {exc}", flush=True)

def mark_result(status, verified=False):
    path = f"control/kick-live-results/{SESSION_ID}.json"
    result = github_get_json(path) or {}
    result.update({"platform":"kick","status":status,"title":TITLE,"description":DESCRIPTION,
                   "session_id":SESSION_ID,"segment_index":SEGMENT_INDEX,"github_run_id":RUN_ID,
                   "github_run_url":RUN_URL,"encoder_resolution":"1920x1080","encoder_fps":60,
                   "encoder_bitrate_kbps":8000,"encoder_connected":status in {"starting","live"},
                   "kick_verified":verified,"updated_at":iso_now()})
    if verified:
        result["live_at"] = iso_now()
    if status in {"starting","live"}:
        for key in ("error_message","completed_at","failed_at"):
            result.pop(key, None)
    github_put_json(path, result, f"peter-lofi: Kick {status} {SESSION_ID} segment {SEGMENT_INDEX}")

def replace_previous_session():
    if SEGMENT_INDEX != 1:
        return
    active = github_get_json("control/kick-active.json") or {}
    old = active.get("session_id")
    if old and old != SESSION_ID and active.get("status") == "live":
        github_put_json(f"control/kick-live-stop/{old}.json",
            {"stop":True,"reason":"replaced_by_new_mediaforge_live","replacement_session_id":SESSION_ID,"requested_at":iso_now()},
            f"peter-lofi: stop replaced Kick live {old}")
        for _ in range(40):
            if kick_state() == "offline":
                break
            time.sleep(2)
    github_put_json("control/kick-active.json",
        {"session_id":SESSION_ID,"status":"live","segment_index":SEGMENT_INDEX,"run_id":RUN_ID,"updated_at":iso_now()},
        f"peter-lofi: active Kick session {SESSION_ID}")

def mark_ready():
    github_put_json(f"control/kick-prewarm/{SESSION_ID}-{SEGMENT_INDEX}.json",
        {"ready":True,"platform":"kick","session_id":SESSION_ID,"segment_index":SEGMENT_INDEX,
         "run_id":RUN_ID,"run_url":RUN_URL,"prepared_at":iso_now()},
        f"peter-lofi: Kick segment ready {SESSION_ID} {SEGMENT_INDEX}")
    print(f"Kick segment {SEGMENT_INDEX} prewarmed.", flush=True)

def wait_takeover():
    if SEGMENT_INDEX <= 1:
        return
    path = f"control/live-takeover/{SESSION_ID}-{SEGMENT_INDEX}.json"
    offline_streak = 0
    started = time.time()
    while True:
        state = github_get_json(path)
        if state and state.get("takeover") is True and int(state.get("from_segment_index") or 0) == SEGMENT_INDEX - 1:
            print("Kick takeover signal received.", flush=True)
            return
        if time.time() - started >= 8:
            ks = kick_state()
            offline_streak = offline_streak + 1 if ks == "offline" else 0
            if offline_streak >= 2:
                print("Legacy predecessor offline; taking over.", flush=True)
                return
        time.sleep(1)

def target():
    base = KICK_STREAM_URL or KICK_RTMP_URL or "rtmps://fa723fc1b171.global-contribute.live-video.net:443/app"
    return f"{base.rstrip('/')}/{KICK_STREAM_KEY}"

def stop_processes():
    global encoder, feeder, encoder_log, feeder_log
    if encoder and encoder.poll() is None:
        try: encoder.send_signal(signal.SIGINT)
        except Exception: pass
    if feeder and feeder.poll() is None:
        try: feeder.terminate()
        except Exception: pass
    end = time.time() + 2.5
    while time.time() < end and encoder and encoder.poll() is None:
        time.sleep(.1)
    if encoder and encoder.poll() is None: encoder.kill()
    if feeder and feeder.poll() is None: feeder.kill()
    for h in (encoder_log, feeder_log):
        try:
            if h: h.close()
        except Exception: pass
    encoder = feeder = encoder_log = feeder_log = None

def start_encoder(loop):
    global encoder, feeder, encoder_log, feeder_log
    stop_processes()
    fifo = BUILD / "audio.pcm"
    try: fifo.unlink()
    except FileNotFoundError: pass
    os.mkfifo(fifo)
    feeder_log = open(BUILD/"audio-feeder.log","ab",buffering=0)
    # Keep both ends open during launch so FIFO setup cannot deadlock.
    fifo_fd = os.open(fifo, os.O_RDWR)
    feeder = subprocess.Popen([sys.executable,"scripts/mediaforge_live_audio_feeder.py",
        "--session-id",SESSION_ID,"--fallback-b64",TRACK_URLS_B64,"--segment-index",str(SEGMENT_INDEX)],
        stdout=fifo_fd, stderr=feeder_log)
    encoder_log = open(BUILD/"ffmpeg.log","ab",buffering=0)
    encoder = subprocess.Popen(["ffmpeg","-hide_banner","-loglevel","warning","-re","-stream_loop","-1","-i",str(loop),
        "-thread_queue_size","512","-f","s16le","-ar","48000","-ac","2","-i",str(fifo),
        "-map","0:v:0","-map","1:a:0",
        "-vf","scale=1280:720:force_original_aspect_ratio=decrease:flags=lanczos,pad=1280:720:(ow-iw)/2:(oh-ih)/2,setsar=1,fps=30,format=yuv420p",
        "-r","30","-s:v","1280x720","-pix_fmt","yuv420p","-c:v","libx264","-preset","superfast","-tune","zerolatency",
        "-profile:v","main","-level:v","3.1","-b:v","3500k","-minrate","3500k","-maxrate","3500k","-bufsize","7000k",
        "-g","60","-keyint_min","60","-sc_threshold","0","-x264-params","nal-hrd=cbr:force-cfr=1",
        "-c:a","aac","-b:a","160k","-ar","48000","-ac","2","-flvflags","no_duration_filesize","-f","flv",target()],
        stdout=encoder_log, stderr=encoder_log)
    os.close(fifo_fd)
    print(f"Kick encoder started pid={encoder.pid}.", flush=True)

def assert_processes():
    if not encoder or encoder.poll() is not None:
        tail = ""
        try:
            p = BUILD / "ffmpeg.log"
            if p.exists():
                raw = p.read_text(encoding="utf-8", errors="replace")
                tail = "\n".join(raw.splitlines()[-80:])
        except Exception:
            pass
        if tail:
            print("=== KICK FFMPEG LOG TAIL ===", file=sys.stderr, flush=True)
            print(tail, file=sys.stderr, flush=True)
            print("=== END KICK FFMPEG LOG TAIL ===", file=sys.stderr, flush=True)
        raise RuntimeError("Kick FFmpeg encoder stopped")
    if not feeder or feeder.poll() is not None:
        raise RuntimeError("Kick audio feeder stopped")

def verify_encoder():
    time.sleep(15)
    assert_processes()
    verified = unavailable = False
    for n in range(1,13):
        assert_processes()
        state = kick_state()
        print(f"Kick verification {n}/12: {state}", flush=True)
        if state == "online":
            verified = True
            break
        if state == "unavailable":
            unavailable = True
        time.sleep(5)
    if not verified:
        print("::warning::Kick not positively verified; preserving healthy encoder.", flush=True)
    mark_result("live" if verified else "starting", verified)

def successor_ready():
    state = github_get_json(f"control/kick-prewarm/{SESSION_ID}-{SEGMENT_INDEX+1}.json")
    if not state or state.get("ready") is not True:
        return False
    run_id = state.get("run_id")
    if not run_id: return True
    try:
        run = api_request("GET", f"actions/runs/{int(run_id)}")
        return run.get("status") in {"queued","in_progress"}
    except Exception:
        return True

def stop_requested():
    state = github_get_json(f"control/kick-live-stop/{SESSION_ID}.json")
    return bool(state and state.get("stop") is True)

def capture_handoff(stop_epoch):
    state = github_get_json(f"control/live-now-playing/{SESSION_ID}.json") or {}
    started = parse_ts(state.get("started_at"))
    position = max(0.0, stop_epoch-started) if started is not None else float(state.get("resume_offset_seconds") or 0)
    return {"platform":"kick","session_id":SESSION_ID,"segment_index":SEGMENT_INDEX+1,
            "from_segment_index":SEGMENT_INDEX,"to_segment_index":SEGMENT_INDEX+1,"takeover":True,
            "track_id":state.get("track_id"),"title":state.get("title"),"url":state.get("url"),
            "track_started_at":state.get("started_at"),"position_seconds":round(position,3),
            "stop_epoch":round(stop_epoch,6),"signaled_at":iso_now(),"source_run_id":RUN_ID,
            "handoff_protocol":"kick-v2-prewarm"}

def run_segment(loop):
    chain = DURATION_MINUTES == 0 or DURATION_MINUTES > 300
    seconds = 300*60 if DURATION_MINUTES == 0 else min(300,DURATION_MINUTES)*60
    deadline = int(time.time()) + seconds
    prewarm_at = deadline - min(600,max(30,seconds//2))
    hard_deadline = deadline + 1200
    dispatched = False
    last_dispatch = 0
    reconnects = 0
    while True:
        now = int(time.time())
        if stop_requested():
            stop_processes()
            return False
        if not encoder or encoder.poll() is not None or not feeder or feeder.poll() is not None:
            reconnects += 1
            if reconnects > 8: raise RuntimeError("Kick reconnect limit reached")
            print(f"Kick reconnect {reconnects}/8.", flush=True)
            start_encoder(loop)
            time.sleep(12)
        if chain and not dispatched and now >= prewarm_at:
            dispatched = dispatch_next(); last_dispatch = now
        if now >= deadline:
            if not chain:
                stop_processes(); return False
            if not dispatched:
                dispatched = dispatch_next(); last_dispatch = now
            if successor_ready():
                print(f"Kick successor {SEGMENT_INDEX+1} ready; releasing key.", flush=True)
                stop_epoch = time.time()
                stop_processes()
                github_put_json(f"control/live-takeover/{SESSION_ID}-{SEGMENT_INDEX+1}.json",
                    capture_handoff(stop_epoch), f"peter-lofi: Kick takeover {SESSION_ID} {SEGMENT_INDEX+1}")
                return True
            if now-last_dispatch >= 60:
                dispatch_next(); last_dispatch = now
            if now >= hard_deadline:
                raise RuntimeError("Kick successor not ready after 20-minute safety extension")
            print("Kick successor not ready; current encoder remains online.", flush=True)
        time.sleep(5)

def mark_complete():
    result = github_get_json(f"control/kick-live-results/{SESSION_ID}.json") or {}
    result.update({"status":"completed","encoder_connected":False,"completed_at":iso_now()})
    github_put_json(f"control/kick-live-results/{SESSION_ID}.json",result,f"peter-lofi: Kick complete {SESSION_ID}")

def record_failure(exc):
    try:
        result = github_get_json(f"control/kick-live-results/{SESSION_ID}.json") or {}
        result.update({"platform":"kick","status":"failed","session_id":SESSION_ID,"segment_index":SEGMENT_INDEX,
                       "github_run_id":RUN_ID,"github_run_url":RUN_URL,"encoder_connected":False,
                       "error_message":str(exc),"failed_at":iso_now()})
        github_put_json(f"control/kick-live-results/{SESSION_ID}.json",result,f"peter-lofi: Kick failed {SESSION_ID}")
    except Exception as nested:
        print(f"Could not record failure: {nested}", flush=True)

def validate():
    import re
    if not re.fullmatch(r"[0-9a-fA-F-]{36}",SESSION_ID): raise RuntimeError("Invalid session_id")
    if not 0 <= DURATION_MINUTES <= 10080: raise RuntimeError("duration_minutes out of range")
    if SEGMENT_INDEX < 1: raise RuntimeError("segment_index must be >=1")
    if not KICK_STREAM_KEY: raise RuntimeError("Missing KICK_STREAM_KEY")
    urls = json.loads(base64.b64decode(TRACK_URLS_B64).decode())
    if not isinstance(urls,list) or not urls: raise RuntimeError("No track URLs")

def main():
    validate()
    loop = prepare_media()
    apply_title()
    if SEGMENT_INDEX == 1:
        replace_previous_session()
    else:
        mark_ready()
        wait_takeover()
    mark_result("starting",False)
    start_encoder(loop)
    verify_encoder()
    chained = run_segment(loop)
    if not chained: mark_complete()

if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"FATAL: {exc}", file=sys.stderr, flush=True)
        try: stop_processes()
        except Exception: pass
        record_failure(exc)
        raise
