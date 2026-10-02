#!/usr/bin/env python3
"""MediaForge Kick live segment V3 — continuity first.

The RTMP encoder is the data plane and GitHub is only a control plane. A GitHub
API outage/rate-limit must never stop a healthy encoder. Successors are prepared
early and the predecessor only releases the stream key after the takeover state
has been successfully persisted for a future local cutover instant.
"""
import json
import time
import urllib.error
import urllib.request

import mediaforge_kick_segment_v2 as core
import mediaforge_control_plane as cp

_stop_cache = {"checked": 0.0, "value": False}
_ready_cache = {"checked": 0.0, "value": False}
RAW_BASE = f"https://raw.githubusercontent.com/{core.REPO}/main"


def _safe_get(path):
    """Read public control state without spending the GitHub installation API quota."""
    try:
        req = urllib.request.Request(
            f"{RAW_BASE}/{path}?ts={int(time.time() * 1000)}",
            headers={"User-Agent": "MediaForge-Kick-V3-Control"},
        )
        with urllib.request.urlopen(req, timeout=12) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None
        print(f"::warning::Kick raw control read HTTP {exc.code} for {path}; treating as unavailable.", flush=True)
        return None
    except Exception as exc:
        print(f"::warning::Kick raw control read ignored for {path}: {exc}", flush=True)
        return None


def _safe_put(path, payload, message):
    try:
        core.github_put_json(path, payload, message)
        return True
    except Exception as exc:
        print(f"::warning::Kick control-plane write deferred for {path}: {exc}", flush=True)
        return False


def stop_requested_v3():
    now = time.time()
    if now - _stop_cache["checked"] < 60:
        return _stop_cache["value"]
    _stop_cache["checked"] = now
    state = _safe_get(f"control/kick-live-stop/{core.SESSION_ID}.json")
    if state is not None:
        _stop_cache["value"] = bool(state.get("stop") is True)
    return _stop_cache["value"]


def successor_ready_v3():
    now = time.time()
    if now - _ready_cache["checked"] < 20:
        return _ready_cache["value"]
    _ready_cache["checked"] = now
    state = _safe_get(f"control/kick-prewarm/{core.SESSION_ID}-{core.SEGMENT_INDEX + 1}.json")
    if not state or state.get("ready") is not True:
        _ready_cache["value"] = False
        return False
    if int(state.get("segment_index") or 0) != core.SEGMENT_INDEX + 1:
        _ready_cache["value"] = False
        return False
    # The ready marker is written only after the successor finished media preparation
    # and entered the takeover wait loop. Do not require a second rate-limited Actions API read.
    _ready_cache["value"] = True
    return True


def mark_result_v3(status, verified=False):
    path = f"control/kick-live-results/{core.SESSION_ID}.json"
    result = _safe_get(path) or {}
    result.update({
        "platform": "kick", "status": status, "title": core.TITLE,
        "description": core.DESCRIPTION, "session_id": core.SESSION_ID,
        "segment_index": core.SEGMENT_INDEX, "github_run_id": core.RUN_ID,
        "github_run_url": core.RUN_URL, "encoder_resolution": "1920x1080",
        "encoder_fps": 60, "encoder_bitrate_kbps": 8000,
        "encoder_connected": status in {"starting", "live"},
        "kick_verified": verified, "updated_at": core.iso_now(),
    })
    if verified:
        result["live_at"] = core.iso_now()
    if status in {"starting", "live"}:
        for key in ("error_message", "completed_at", "failed_at"):
            result.pop(key, None)
    _safe_put(path, result, f"peter-lofi: Kick {status} {core.SESSION_ID} segment {core.SEGMENT_INDEX}")


def mark_ready_v3():
    payload = {
        "ready": True, "platform": "kick", "session_id": core.SESSION_ID,
        "segment_index": core.SEGMENT_INDEX, "run_id": core.RUN_ID,
        "run_url": core.RUN_URL, "prepared_at": core.iso_now(),
    }
    path = f"control/kick-prewarm/{core.SESSION_ID}-{core.SEGMENT_INDEX}.json"
    while True:
        if _safe_put(path, payload, f"peter-lofi: Kick segment ready {core.SESSION_ID} {core.SEGMENT_INDEX}"):
            print(f"Kick segment {core.SEGMENT_INDEX} prewarmed.", flush=True)
            return
        print("Kick readiness publication unavailable; retrying without touching predecessor.", flush=True)
        time.sleep(30)


def wait_takeover_v3():
    if core.SEGMENT_INDEX <= 1:
        return
    path = f"control/live-takeover/{core.SESSION_ID}-{core.SEGMENT_INDEX}.json"
    print(f"Kick segment {core.SEGMENT_INDEX} prewarmed; waiting for continuity-first takeover.", flush=True)
    offline_streak = 0
    last_kick_check = 0.0
    while True:
        state = _safe_get(path)
        if state and state.get("takeover") is True and int(state.get("from_segment_index") or 0) == core.SEGMENT_INDEX - 1:
            cutover = float(state.get("cutover_epoch") or 0.0)
            if cutover > time.time():
                print(f"Kick scheduled cutover armed for {cutover:.3f}; waiting locally.", flush=True)
                while time.time() < cutover:
                    time.sleep(min(0.05, max(0.005, cutover - time.time())))
            print("Kick takeover released.", flush=True)
            return

        now = time.time()
        if now - last_kick_check >= 20:
            last_kick_check = now
            ks = core.kick_state()
            offline_streak = offline_streak + 1 if ks == "offline" else 0
            if offline_streak >= 2:
                print("Kick predecessor confirmed offline; emergency successor takeover.", flush=True)
                return
        time.sleep(10)


def replace_previous_session_v3():
    if core.SEGMENT_INDEX != 1:
        return
    active = _safe_get("control/kick-active.json") or {}
    old = active.get("session_id")
    ks = core.kick_state()
    if old and old != core.SESSION_ID and ks == "online":
        _safe_put(
            f"control/kick-live-stop/{old}.json",
            {"stop": True, "reason": "replaced_by_new_mediaforge_live", "replacement_session_id": core.SESSION_ID, "requested_at": core.iso_now()},
            f"peter-lofi: stop replaced Kick live {old}",
        )
        for _ in range(90):
            if core.kick_state() == "offline":
                break
            time.sleep(2)
        else:
            raise RuntimeError("Existing Kick encoder still appears online; refusing duplicate RTMP publisher")
    elif ks == "unavailable":
        print("::warning::Kick status API unavailable; recovery will proceed with one encoder.", flush=True)

    _safe_put(
        "control/kick-active.json",
        {"session_id": core.SESSION_ID, "status": "starting", "segment_index": core.SEGMENT_INDEX, "run_id": core.RUN_ID, "updated_at": core.iso_now()},
        f"peter-lofi: active Kick session {core.SESSION_ID}",
    )


def _capture_takeover(cutover_epoch):
    state = _safe_get(f"control/live-now-playing/{core.SESSION_ID}.json") or {}
    started = core.parse_ts(state.get("started_at"))
    position = max(0.0, cutover_epoch - started) if started is not None else float(state.get("resume_offset_seconds") or 0.0)
    return {
        "platform": "kick", "session_id": core.SESSION_ID,
        "segment_index": core.SEGMENT_INDEX + 1,
        "from_segment_index": core.SEGMENT_INDEX,
        "to_segment_index": core.SEGMENT_INDEX + 1, "takeover": True,
        "track_id": state.get("track_id"), "title": state.get("title"),
        "url": state.get("url"), "track_started_at": state.get("started_at"),
        "position_seconds": round(position, 3),
        "cutover_epoch": round(cutover_epoch, 6), "signaled_at": core.iso_now(),
        "source_run_id": core.RUN_ID, "handoff_mode": "scheduled-single-key",
        "handoff_protocol": "kick-v3-continuity-first",
    }



def dispatch_next_v3():
    """Trigger the successor through git transport instead of the rate-limited Actions REST API."""
    remain = 0 if core.DURATION_MINUTES == 0 else max(1, core.DURATION_MINUTES - 300)
    path = f"control/kick-successor/{core.SESSION_ID}-{core.SEGMENT_INDEX + 1}.json"
    payload = {
        "session_id": core.SESSION_ID,
        "track_urls_b64": core.TRACK_URLS_B64,
        "duration_minutes": str(remain),
        "title": core.TITLE,
        "description": core.DESCRIPTION,
        "thumbnail_url": core.THUMBNAIL_URL,
        "loop_url": core.LOOP_URL,
        "segment_index": str(core.SEGMENT_INDEX + 1),
        "requested_at": core.iso_now(),
        "source_run_id": core.RUN_ID,
        "protocol": "kick-v4-git-successor",
    }
    ok = cp.git_put_json(
        path,
        payload,
        f"live: trigger Kick successor {core.SESSION_ID} {core.SEGMENT_INDEX + 1}",
    )
    print(
        f"Kick successor {core.SEGMENT_INDEX + 1} " + ("triggered by git push." if ok else "trigger failed; predecessor stays online."),
        flush=True,
    )
    return ok

def run_segment_v3(loop):
    chain = core.DURATION_MINUTES == 0 or core.DURATION_MINUTES > 300
    seconds = 300 * 60 if core.DURATION_MINUTES == 0 else min(300, core.DURATION_MINUTES) * 60
    deadline = int(time.time()) + seconds
    prewarm_at = deadline - min(900, max(60, seconds // 2))
    dispatched = False
    last_dispatch = 0
    reconnects = 0

    while True:
        now = int(time.time())
        if stop_requested_v3():
            core.stop_processes()
            return False

        if not core.encoder or core.encoder.poll() is not None or not core.feeder or core.feeder.poll() is not None:
            reconnects += 1
            if reconnects > 20:
                raise RuntimeError("Kick local encoder reconnect limit reached")
            print(f"Kick local encoder reconnect {reconnects}/20.", flush=True)
            core.start_encoder(loop)
            time.sleep(12)
            continue

        if chain and not dispatched and now >= prewarm_at:
            dispatched = dispatch_next_v3()
            last_dispatch = now

        if now >= deadline:
            if not chain:
                core.stop_processes()
                return False
            if not dispatched:
                dispatched = dispatch_next_v3()
                last_dispatch = now

            if successor_ready_v3():
                cutover = time.time() + 30.0
                payload = _capture_takeover(cutover)
                path = f"control/live-takeover/{core.SESSION_ID}-{core.SEGMENT_INDEX + 1}.json"
                if _safe_put(path, payload, f"peter-lofi: Kick V3 takeover {core.SESSION_ID} {core.SEGMENT_INDEX + 1}"):
                    print(f"Kick takeover committed; predecessor remains online until {cutover:.3f}.", flush=True)
                    while time.time() < cutover:
                        core.assert_processes()
                        time.sleep(min(0.05, max(0.005, cutover - time.time())))
                    core.stop_processes()
                    return True
                print("Kick takeover commit failed; predecessor remains online. No cutover performed.", flush=True)
            elif now - last_dispatch >= 180:
                core.dispatch_next()
                last_dispatch = now

            print("Kick successor not safely coordinated; current encoder remains online.", flush=True)

        time.sleep(5)


def mark_complete_v3():
    path = f"control/kick-live-results/{core.SESSION_ID}.json"
    result = _safe_get(path) or {}
    result.update({"status": "completed", "encoder_connected": False, "completed_at": core.iso_now()})
    _safe_put(path, result, f"peter-lofi: Kick complete {core.SESSION_ID}")


def record_failure_v3(exc):
    path = f"control/kick-live-results/{core.SESSION_ID}.json"
    result = _safe_get(path) or {}
    result.update({
        "platform": "kick", "status": "failed", "session_id": core.SESSION_ID,
        "segment_index": core.SEGMENT_INDEX, "github_run_id": core.RUN_ID,
        "github_run_url": core.RUN_URL, "encoder_connected": False,
        "error_message": str(exc), "failed_at": core.iso_now(),
    })
    _safe_put(path, result, f"peter-lofi: Kick failed {core.SESSION_ID}")


def main_v3():
    """Critical ordering: after takeover, start RTMP first; publish state second."""
    core.validate()
    loop = core.prepare_media()
    core.apply_title()
    if core.SEGMENT_INDEX == 1:
        replace_previous_session_v3()
    else:
        mark_ready_v3()
        wait_takeover_v3()

    # No GitHub API request is allowed between cutover and encoder start.
    core.start_encoder(loop)
    mark_result_v3("starting", False)
    core.verify_encoder()
    chained = run_segment_v3(loop)
    if not chained:
        mark_complete_v3()


core.stop_requested = stop_requested_v3
core.successor_ready = successor_ready_v3
core.mark_result = mark_result_v3
core.mark_ready = mark_ready_v3
core.wait_takeover = wait_takeover_v3
core.replace_previous_session = replace_previous_session_v3
core.run_segment = run_segment_v3
core.dispatch_next = dispatch_next_v3
core.mark_complete = mark_complete_v3
core.record_failure = record_failure_v3


if __name__ == "__main__":
    try:
        main_v3()
    except Exception as exc:
        print(f"FATAL: {exc}", file=core.sys.stderr, flush=True)
        try:
            core.stop_processes()
        except Exception:
            pass
        try:
            record_failure_v3(exc)
        except Exception as nested:
            print(f"failure recorder also failed: {nested}", file=core.sys.stderr, flush=True)
        raise
