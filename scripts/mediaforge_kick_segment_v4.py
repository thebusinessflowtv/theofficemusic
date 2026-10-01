#!/usr/bin/env python3
"""MediaForge Kick V4 — git-control continuity runner.

V4 keeps Kick V3 RTMP/reconnect logic while removing GitHub REST from all
continuity-critical state and successor dispatch.
"""
import json
import pathlib

import mediaforge_kick_segment_v3 as v3
import mediaforge_control_plane as cp

core = v3.core


def raw_get(path):
    return cp.raw_get(core.REPO, path, user_agent="MediaForge-Kick-V4")


def git_put(path, payload, message):
    return cp.git_put_json(path, payload, message)


def local_now_playing():
    p = pathlib.Path("build/now-playing.json")
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def successor_ready_v4():
    state = raw_get(f"control/kick-prewarm/{core.SESSION_ID}-{core.SEGMENT_INDEX + 1}.json")
    return bool(
        state
        and state.get("ready") is True
        and int(state.get("segment_index") or 0) == core.SEGMENT_INDEX + 1
    )


def dispatch_next_v4():
    remain = 0 if core.DURATION_MINUTES == 0 else max(1, core.DURATION_MINUTES - 300)
    path = f"control/live-successor-kick/{core.SESSION_ID}-{core.SEGMENT_INDEX + 1}.json"
    payload = {
        "session_id": core.SESSION_ID,
        "track_urls_b64": core.TRACK_URLS_B64,
        "duration_minutes": remain,
        "title": core.TITLE,
        "description": core.DESCRIPTION,
        "thumbnail_url": core.THUMBNAIL_URL,
        "loop_url": core.LOOP_URL,
        "segment_index": core.SEGMENT_INDEX + 1,
        "requested_at": core.iso_now(),
        "source_run_id": core.RUN_ID,
        "protocol": "kick-v4-git-trigger",
    }
    ok = git_put(path, payload, f"live: trigger Kick successor {core.SESSION_ID} {core.SEGMENT_INDEX + 1}")
    print(
        f"Kick successor {core.SEGMENT_INDEX + 1} " + ("triggered by git push." if ok else "trigger failed; predecessor stays online."),
        flush=True,
    )
    return ok


def capture_takeover_v4(cutover_epoch):
    state = local_now_playing() or raw_get(f"control/live-now-playing/{core.SESSION_ID}.json") or {}
    started = core.parse_ts(state.get("started_at"))
    position = max(0.0, cutover_epoch - started) if started is not None else float(state.get("resume_offset_seconds") or 0.0)
    return {
        "platform": "kick",
        "session_id": core.SESSION_ID,
        "segment_index": core.SEGMENT_INDEX + 1,
        "from_segment_index": core.SEGMENT_INDEX,
        "to_segment_index": core.SEGMENT_INDEX + 1,
        "takeover": True,
        "track_id": state.get("track_id"),
        "title": state.get("title"),
        "url": state.get("url"),
        "track_started_at": state.get("started_at"),
        "position_seconds": round(position, 3),
        "cutover_epoch": round(cutover_epoch, 6),
        "signaled_at": core.iso_now(),
        "source_run_id": core.RUN_ID,
        "handoff_mode": "scheduled-single-key",
        "handoff_protocol": "kick-v4-git-control",
    }


# Patch V3 globals and inherited V2 core.
v3._safe_get = raw_get
v3._safe_put = git_put
v3.successor_ready_v3 = successor_ready_v4
v3._capture_takeover = capture_takeover_v4
core.github_get_json = raw_get
core.github_put_json = git_put
core.dispatch_next = dispatch_next_v4
core.successor_ready = successor_ready_v4


if __name__ == "__main__":
    try:
        v3.main_v3()
    except Exception as exc:
        print(f"FATAL Kick V4: {exc}", file=core.sys.stderr, flush=True)
        try:
            core.stop_processes()
        except Exception:
            pass
        try:
            v3.record_failure_v3(exc)
        except Exception as nested:
            print(f"failure recorder warning: {nested}", file=core.sys.stderr, flush=True)
        raise
