#!/usr/bin/env python3
"""MediaForge Twitch live runner.

Uses the same Peter Lofi dynamic audio feeder and visual loop pipeline as Kick,
but publishes to Twitch over RTMPS. The stream key is read only from the
TWITCH_STREAM_KEY GitHub secret.

For continuous lives, a successor runner is prepared before the hosted-runner
limit and takes over the same Twitch stream key only after the predecessor has
released it.
"""
import json
import os
import pathlib
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request

import mediaforge_kick_segment_v2 as core

TWITCH_STREAM_KEY = os.environ.get("TWITCH_STREAM_KEY", "").strip()
TWITCH_INGEST_URL = os.environ.get("TWITCH_INGEST_URL", "rtmps://live.twitch.tv:443/app").strip().rstrip("/")
RAW_BASE = f"https://raw.githubusercontent.com/{core.REPO}/main"
BUILD = core.BUILD

def raw_get(path):
    try:
        req = urllib.request.Request(
            f"{RAW_BASE}/{path}?ts={int(time.time()*1000)}",
            headers={"User-Agent": "MediaForge-Twitch-Control", "Cache-Control": "no-cache"},
        )
        with urllib.request.urlopen(req, timeout=12) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        print(f"::warning::Twitch raw read HTTP {e.code} for {path}", flush=True)
        return None
    except Exception as e:
        print(f"::warning::Twitch control read unavailable for {path}: {e}", flush=True)
        return None

def safe_put(path, payload, message):
    try:
        core.github_put_json(path, payload, message)
        return True
    except Exception as e:
        print(f"::warning::Twitch state write failed for {path}: {e}", flush=True)
        return False

def target():
    return f"{TWITCH_INGEST_URL}/{TWITCH_STREAM_KEY}"

def validate():
    core.validate()
    if not TWITCH_STREAM_KEY:
        raise RuntimeError("Missing TWITCH_STREAM_KEY")

def start_encoder(loop):
    core.stop_processes()
    fifo = BUILD / "audio.pcm"
    try:
        fifo.unlink()
    except FileNotFoundError:
        pass
    os.mkfifo(fifo)

    core.feeder_log = open(BUILD / "twitch-audio-feeder.log", "ab", buffering=0)
    fifo_fd = os.open(fifo, os.O_RDWR)
    core.feeder = subprocess.Popen(
        [
            sys.executable,
            "scripts/mediaforge_live_audio_feeder.py",
            "--session-id", core.SESSION_ID,
            "--fallback-b64", core.TRACK_URLS_B64,
            "--segment-index", str(core.SEGMENT_INDEX),
        ],
        stdout=fifo_fd,
        stderr=core.feeder_log,
    )

    core.encoder_log = open(BUILD / "twitch-ffmpeg.log", "ab", buffering=0)
    core.encoder = subprocess.Popen(
        [
            "ffmpeg", "-hide_banner", "-loglevel", "warning",
            "-re", "-stream_loop", "-1", "-i", str(loop),
            "-thread_queue_size", "1024",
            "-f", "s16le", "-ar", "48000", "-ac", "2", "-i", str(fifo),
            "-map", "0:v:0", "-map", "1:a:0",
            "-vf", "scale=1920:1080:force_original_aspect_ratio=decrease:flags=lanczos,pad=1920:1080:(ow-iw)/2:(oh-ih)/2,setsar=1,fps=30,format=yuv420p",
            "-r", "30", "-s:v", "1920x1080", "-pix_fmt", "yuv420p",
            "-c:v", "libx264", "-preset", "veryfast", "-tune", "zerolatency",
            "-profile:v", "high", "-level:v", "4.1",
            "-b:v", "6000k", "-minrate", "6000k", "-maxrate", "6000k", "-bufsize", "12000k",
            "-g", "60", "-keyint_min", "60", "-sc_threshold", "0",
            "-x264-params", "nal-hrd=cbr:force-cfr=1",
            "-c:a", "aac", "-b:a", "160k", "-ar", "48000", "-ac", "2",
            "-flvflags", "no_duration_filesize", "-f", "flv", target(),
        ],
        stdout=core.encoder_log,
        stderr=core.encoder_log,
    )
    os.close(fifo_fd)
    print(f"Twitch encoder started pid={core.encoder.pid}", flush=True)

def assert_processes():
    if not core.encoder or core.encoder.poll() is not None:
        tail = ""
        try:
            tail = (BUILD / "twitch-ffmpeg.log").read_text(errors="replace")[-3000:]
        except Exception:
            pass
        raise RuntimeError("Twitch FFmpeg encoder stopped unexpectedly. " + tail)
    if not core.feeder or core.feeder.poll() is not None:
        raise RuntimeError("Twitch audio feeder stopped unexpectedly")

def mark_result(status, verified=False, error_message=None):
    path = f"control/twitch-live-results/{core.SESSION_ID}.json"
    result = raw_get(path) or {}
    result.update({
        "platform": "twitch",
        "status": status,
        "title": core.TITLE,
        "description": core.DESCRIPTION,
        "session_id": core.SESSION_ID,
        "segment_index": core.SEGMENT_INDEX,
        "github_run_id": core.RUN_ID,
        "github_run_url": core.RUN_URL,
        "encoder_resolution": "1920x1080",
        "encoder_fps": 30,
        "encoder_bitrate_kbps": 6000,
        "encoder_connected": status in {"starting", "live"},
        "twitch_ingest_verified": verified,
        "updated_at": core.iso_now(),
    })
    if verified:
        result["live_at"] = result.get("live_at") or core.iso_now()
    if error_message:
        result["error_message"] = str(error_message)
    elif status in {"starting", "live"}:
        for key in ("error_message", "failed_at", "completed_at"):
            result.pop(key, None)
    safe_put(path, result, f"peter-lofi: Twitch {status} {core.SESSION_ID} segment {core.SEGMENT_INDEX}")

def mark_active(status):
    safe_put(
        "control/twitch-active.json",
        {
            "platform": "twitch",
            "session_id": core.SESSION_ID,
            "status": status,
            "segment_index": core.SEGMENT_INDEX,
            "run_id": core.RUN_ID,
            "title": core.TITLE,
            "updated_at": core.iso_now(),
        },
        f"peter-lofi: Twitch active {core.SESSION_ID}",
    )

def verify_encoder():
    # A wrong/expired stream key causes Twitch RTMP to close FFmpeg quickly.
    # Surviving this window proves the RTMPS publisher was accepted by ingest.
    for n in range(1, 5):
        time.sleep(5)
        assert_processes()
        print(f"Twitch ingest health {n}/4: encoder alive", flush=True)
    mark_result("live", True)
    mark_active("live")

def stop_requested():
    state = raw_get(f"control/twitch-live-stop/{core.SESSION_ID}.json")
    return bool(state and state.get("stop") is True)

def mark_ready():
    while True:
        ok = safe_put(
            f"control/twitch-prewarm/{core.SESSION_ID}-{core.SEGMENT_INDEX}.json",
            {
                "ready": True,
                "platform": "twitch",
                "session_id": core.SESSION_ID,
                "segment_index": core.SEGMENT_INDEX,
                "run_id": core.RUN_ID,
                "run_url": core.RUN_URL,
                "prepared_at": core.iso_now(),
            },
            f"peter-lofi: Twitch segment ready {core.SESSION_ID} {core.SEGMENT_INDEX}",
        )
        if ok:
            return
        time.sleep(20)

def successor_ready():
    s = raw_get(f"control/twitch-prewarm/{core.SESSION_ID}-{core.SEGMENT_INDEX+1}.json")
    return bool(s and s.get("ready") is True and int(s.get("segment_index") or 0) == core.SEGMENT_INDEX + 1)

def wait_takeover():
    if core.SEGMENT_INDEX <= 1:
        return
    path = f"control/twitch-takeover/{core.SESSION_ID}-{core.SEGMENT_INDEX}.json"
    print(f"Twitch segment {core.SEGMENT_INDEX} prewarmed; waiting for takeover.", flush=True)
    while True:
        s = raw_get(path)
        if s and s.get("takeover") is True and int(s.get("from_segment_index") or 0) == core.SEGMENT_INDEX - 1:
            cut = float(s.get("cutover_epoch") or 0)
            if cut > time.time():
                while time.time() < cut:
                    time.sleep(min(.05, max(.005, cut - time.time())))
            return
        time.sleep(10)

def dispatch_next():
    remain = 0 if core.DURATION_MINUTES == 0 else max(1, core.DURATION_MINUTES - 300)
    inputs = {
        "session_id": core.SESSION_ID,
        "track_urls_b64": core.TRACK_URLS_B64,
        "duration_minutes": str(remain),
        "title": core.TITLE,
        "description": core.DESCRIPTION,
        "thumbnail_url": core.THUMBNAIL_URL,
        "loop_url": core.LOOP_URL,
        "segment_index": str(core.SEGMENT_INDEX + 1),
    }
    try:
        core.api_request(
            "POST",
            "actions/workflows/peter-lofi-twitch-live.yml/dispatches",
            {"ref": "main", "inputs": inputs},
        )
        print(f"Twitch successor {core.SEGMENT_INDEX+1} dispatched.", flush=True)
        return True
    except Exception as e:
        print(f"::warning::Twitch successor dispatch failed: {e}", flush=True)
        return False

def run_segment():
    chain = core.DURATION_MINUTES == 0 or core.DURATION_MINUTES > 300
    seconds = 300 * 60 if core.DURATION_MINUTES == 0 else min(300, core.DURATION_MINUTES) * 60
    deadline = int(time.time()) + seconds
    prewarm_at = deadline - min(900, max(60, seconds // 2))
    dispatched = False
    last_dispatch = 0
    reconnects = 0

    while True:
        now = int(time.time())

        if stop_requested():
            core.stop_processes()
            mark_result("completed", False)
            mark_active("completed")
            return

        if not core.encoder or core.encoder.poll() is not None or not core.feeder or core.feeder.poll() is not None:
            reconnects += 1
            if reconnects > 20:
                raise RuntimeError("Twitch local reconnect limit reached")
            print(f"Twitch reconnect {reconnects}/20", flush=True)
            start_encoder(BUILD / "loop.mp4")
            time.sleep(12)
            continue

        if chain and not dispatched and now >= prewarm_at:
            dispatched = dispatch_next()
            last_dispatch = now

        if now >= deadline:
            if not chain:
                core.stop_processes()
                mark_result("completed", False)
                mark_active("completed")
                return

            if not dispatched:
                dispatched = dispatch_next()
                last_dispatch = now

            if successor_ready():
                cut = time.time() + 30
                payload = {
                    "platform": "twitch",
                    "session_id": core.SESSION_ID,
                    "from_segment_index": core.SEGMENT_INDEX,
                    "to_segment_index": core.SEGMENT_INDEX + 1,
                    "segment_index": core.SEGMENT_INDEX + 1,
                    "takeover": True,
                    "cutover_epoch": round(cut, 6),
                    "signaled_at": core.iso_now(),
                    "source_run_id": core.RUN_ID,
                    "handoff_protocol": "twitch-v1-continuity-first",
                }
                if safe_put(
                    f"control/twitch-takeover/{core.SESSION_ID}-{core.SEGMENT_INDEX+1}.json",
                    payload,
                    f"peter-lofi: Twitch takeover {core.SESSION_ID} {core.SEGMENT_INDEX+1}",
                ):
                    print(f"Twitch takeover armed for {cut:.3f}", flush=True)
                    while time.time() < cut:
                        assert_processes()
                        time.sleep(min(.05, max(.005, cut - time.time())))
                    core.stop_processes()
                    return

            if now - last_dispatch >= 180:
                dispatched = dispatch_next()
                last_dispatch = now

        time.sleep(5)

def main():
    validate()
    loop = core.prepare_media()
    if core.SEGMENT_INDEX > 1:
        mark_ready()
        wait_takeover()

    mark_result("starting", False)
    mark_active("starting")
    start_encoder(loop)
    verify_encoder()
    run_segment()

if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"FATAL Twitch: {exc}", file=sys.stderr, flush=True)
        try:
            core.stop_processes()
        except Exception:
            pass
        mark_result("failed", False, exc)
        mark_active("failed")
        raise
