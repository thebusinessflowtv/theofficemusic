#!/usr/bin/env python3
"""MediaForge YouTube V8 — live data plane independent from GitHub REST quota.

V8 reuses the proven V7 YouTube/FFmpeg logic, but moves live coordination to:
- raw.githubusercontent.com for control reads;
- git transport for durable state writes and successor triggers;
- local build/now-playing.json for the current audio position.

A healthy RTMP encoder is never stopped because GitHub REST is unavailable.
"""
import json
import pathlib
import time

import mediaforge_live_segment_v7 as v7
import mediaforge_control_plane as cp

core = v7.core


def raw_get(path):
    return cp.raw_get(core.REPO, path, user_agent="MediaForge-YouTube-V8")


def git_put(path, payload, message):
    return cp.git_put_json(path, payload, message)


def local_now_playing():
    p = pathlib.Path("build/now-playing.json")
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def successor_ready_v8():
    state = raw_get(f"control/live-ready/{core.SESSION_ID}-{core.SEGMENT_INDEX + 1}.json")
    return bool(
        state
        and state.get("ready") is True
        and int(state.get("segment_index") or 0) == core.SEGMENT_INDEX + 1
    )


def dispatch_next_v8(bid, sid):
    path = f"control/live-successor-youtube/{core.SESSION_ID}-{core.SEGMENT_INDEX + 1}.json"
    payload = {
        "session_id": core.SESSION_ID,
        "track_urls_b64": core.TRACK_URLS_B64,
        "duration_minutes": core.next_duration(),
        "title": core.TITLE,
        "description": core.DESCRIPTION,
        "thumbnail_url": core.THUMBNAIL_URL,
        "loop_url": core.LOOP_URL,
        "resume_broadcast_id": bid,
        "resume_stream_id": sid,
        "segment_index": core.SEGMENT_INDEX + 1,
        "privacy_status": core.PRIVACY_STATUS,
        "segment_seconds_override": core.SEGMENT_SECONDS_OVERRIDE,
        "prewarm_lead_seconds": max(900, core.PREWARM_LEAD_SECONDS),
        "max_segments": core.MAX_SEGMENTS,
        "requested_at": core.iso_now(),
        "source_run_id": core.RUN_ID,
        "protocol": "youtube-v8-git-trigger",
    }
    ok = git_put(path, payload, f"live: trigger YouTube successor {core.SESSION_ID} {core.SEGMENT_INDEX + 1}")
    print(
        f"YouTube successor {core.SEGMENT_INDEX + 1} " + ("triggered by git push." if ok else "trigger failed; predecessor stays online."),
        flush=True,
    )
    return ok


def handoff_state_v8(cutover_epoch, session_started, mode):
    state = local_now_playing() or raw_get(f"control/live-now-playing/{core.SESSION_ID}.json") or {}
    started = core.parse_ts(state.get("started_at"))
    position = max(0.0, cutover_epoch - started) if started is not None else float(state.get("resume_offset_seconds") or 0.0)
    sess_epoch = core.parse_ts(session_started)
    visual = max(0.0, cutover_epoch - sess_epoch) if sess_epoch else 0.0
    return {
        "session_id": core.SESSION_ID,
        "from_segment_index": core.SEGMENT_INDEX,
        "to_segment_index": core.SEGMENT_INDEX + 1,
        "segment_index": core.SEGMENT_INDEX + 1,
        "track_id": state.get("track_id"),
        "title": state.get("title"),
        "url": state.get("url"),
        "track_started_at": state.get("started_at"),
        "position_seconds": round(position, 3),
        "visual_position_seconds": round(visual, 3),
        "cutover_epoch": round(cutover_epoch, 6),
        "takeover": True,
        "handoff_mode": mode,
        "handoff_protocol": "youtube-v8-git-control",
        "signaled_at": core.iso_now(),
        "source_run_id": core.RUN_ID,
    }


# Replace every GitHub state primitive used by the inherited core/V7 functions.
v7._safe_get = raw_get
v7._safe_put = git_put
v7.successor_ready_v7 = successor_ready_v8
v7._handoff_state = handoff_state_v8
core.github_get_json = raw_get
core.github_put_json = git_put
core.github_run_state = lambda _run_id: (None, None)
core.dispatch_next = dispatch_next_v8
core.successor_ready = successor_ready_v8


if __name__ == "__main__":
    try:
        core.main()
    except Exception as exc:
        print(f"FATAL V8: {exc}", file=core.sys.stderr, flush=True)
        try:
            core.stop_processes()
        except Exception:
            pass
        try:
            core.record_failure(exc)
        except Exception as nested:
            print(f"failure recorder warning: {nested}", file=core.sys.stderr, flush=True)
        raise
