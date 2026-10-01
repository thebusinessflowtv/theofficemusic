#!/usr/bin/env python3
import base64
import json
import signal
import time

import mediaforge_live_segment_v3 as v3

core = v3.core


def github_put_json_retry(path, payload, message):
    """Concurrent runners write state safely even when another commit lands at the same moment."""
    last = None
    for attempt in range(1, 9):
        try:
            existing = core.api_request("GET", f"contents/{path}", allow_404=True)
            body = {
                "message": message,
                "content": base64.b64encode(json.dumps(payload, ensure_ascii=False, indent=2).encode()).decode(),
                "branch": "main",
            }
            if existing and existing.get("sha"):
                body["sha"] = existing["sha"]
            return core.api_request("PUT", f"contents/{path}", body)
        except Exception as exc:
            last = exc
            print(f"state write retry {attempt}/8 for {path}: {exc}", flush=True)
            time.sleep(min(4.0, 0.4 * attempt))
    raise RuntimeError(f"Could not write {path} after retries: {last}")


def takeover_state():
    if core.SEGMENT_INDEX <= 1:
        return None
    state = core.github_get_json(f"control/live-takeover/{core.SESSION_ID}-{core.SEGMENT_INDEX}.json")
    if state and state.get("takeover") is True and int(state.get("from_segment_index") or 0) == core.SEGMENT_INDEX - 1:
        return state
    return None


def visual_offset_v4(loop_path, session_started):
    pos = None
    state = takeover_state()
    if state:
        try:
            pos = float(state.get("visual_position_seconds"))
        except Exception:
            pos = None

    if pos is None and core.SEGMENT_INDEX > 1:
        h = core.github_get_json(f"control/live-handoffs/{core.SESSION_ID}.json")
        if h and int(h.get("from_segment_index") or 0) == core.SEGMENT_INDEX - 1:
            try:
                pos = float(h.get("visual_position_seconds") or 0.0)
            except Exception:
                pos = None

    if pos is None:
        epoch = core.parse_ts(session_started)
        pos = max(0.0, time.time() - epoch) if epoch else 0.0

    probe = core.run([
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


def stop_for_handoff():
    """Stop source encoder, anchoring media position at the instant shutdown begins."""
    stop_epoch = time.time()

    if core.encoder and core.encoder.poll() is None:
        try:
            core.encoder.send_signal(signal.SIGINT)
        except Exception:
            pass
    if core.feeder and core.feeder.poll() is None:
        try:
            core.feeder.terminate()
        except Exception:
            pass

    end = time.time() + 3.0
    while time.time() < end and core.encoder and core.encoder.poll() is None:
        time.sleep(0.10)
    if core.encoder and core.encoder.poll() is None:
        core.encoder.kill()
    if core.feeder and core.feeder.poll() is None:
        core.feeder.kill()

    try:
        if core.encoder_log:
            core.encoder_log.close()
    except Exception:
        pass
    try:
        if core.feeder_log:
            core.feeder_log.close()
    except Exception:
        pass

    return stop_epoch


def exact_handoff_state(stop_epoch, session_started):
    state = core.github_get_json(f"control/live-now-playing/{core.SESSION_ID}.json") or {}
    started = core.parse_ts(state.get("started_at"))
    if started is not None:
        position = max(0.0, stop_epoch - started)
    else:
        position = float(state.get("resume_offset_seconds") or 0.0)
    sess_epoch = core.parse_ts(session_started)
    visual = max(0.0, stop_epoch - sess_epoch) if sess_epoch else 0.0
    return {
        "session_id": core.SESSION_ID,
        "from_segment_index": core.SEGMENT_INDEX,
        "to_segment_index": core.SEGMENT_INDEX + 1,
        "track_id": state.get("track_id"),
        "title": state.get("title"),
        "url": state.get("url"),
        "track_started_at": state.get("started_at"),
        "position_seconds": round(position, 3),
        "visual_position_seconds": round(visual, 3),
        "stop_epoch": round(stop_epoch, 6),
        "recorded_at": core.iso_now(),
        "source_run_id": core.RUN_ID,
    }


def run_segment_v4(yt, bid, sid, session_started):
    chain = core.should_chain()
    seg_seconds = core.segment_seconds()
    lead = min(core.PREWARM_LEAD_SECONDS, max(10, seg_seconds // 2))
    start_epoch = int(time.time())
    deadline = start_epoch + seg_seconds
    prewarm_at = deadline - lead
    hard_deadline = deadline + 1200
    prewarm_sent = False
    last_redispatch = 0
    reason = "segment_complete"

    while True:
        core.assert_processes()
        now = int(time.time())

        if core.stop_requested():
            reason = "manual_stop"
            chain = False
            break

        if chain and not prewarm_sent and now >= prewarm_at:
            prewarm_sent = core.dispatch_next(bid, sid)
            last_redispatch = now

        if now >= deadline:
            if not chain:
                break
            if not prewarm_sent:
                prewarm_sent = core.dispatch_next(bid, sid)
                last_redispatch = now
            if core.successor_ready():
                print(f"Successor {core.SEGMENT_INDEX + 1} is ready; performing V4 single-state handoff.", flush=True)
                break
            if now - last_redispatch >= 60:
                core.dispatch_next(bid, sid)
                last_redispatch = now
            if now >= hard_deadline:
                raise RuntimeError("Successor did not become ready before the 20-minute safety extension")
            print("Successor not ready; current encoder remains online.", flush=True)

        time.sleep(5)

    if chain and reason == "segment_complete":
        stop_epoch = stop_for_handoff()
        handoff = exact_handoff_state(stop_epoch, session_started)
        takeover = dict(handoff)
        takeover.update({
            "segment_index": core.SEGMENT_INDEX + 1,
            "takeover": True,
            "signaled_at": core.iso_now(),
            "handoff_protocol": "v4-single-state",
        })

        # Critical path: one write is enough for successor audio + visual resume.
        core.github_put_json(
            f"control/live-takeover/{core.SESSION_ID}-{core.SEGMENT_INDEX + 1}.json",
            takeover,
            f"live: takeover {core.SESSION_ID} {core.SEGMENT_INDEX + 1}",
        )
        print(
            f"Takeover released at audio offset {handoff.get('position_seconds')}s; source encoder is already stopped.",
            flush=True,
        )

        # Audit copy is non-critical and happens only after successor has been released.
        try:
            core.github_put_json(
                f"control/live-handoffs/{core.SESSION_ID}.json",
                handoff,
                f"live: handoff audit {core.SESSION_ID} {core.SEGMENT_INDEX}",
            )
        except Exception as exc:
            print(f"handoff audit warning: {exc}", flush=True)
        return True, reason

    core.stop_processes()
    return False, reason


core.github_put_json = github_put_json_retry
core.visual_offset = visual_offset_v4
core.run_segment = run_segment_v4


if __name__ == "__main__":
    try:
        core.main()
    except Exception as exc:
        print(f"FATAL: {exc}", file=core.sys.stderr, flush=True)
        try:
            core.stop_processes()
        except Exception:
            pass
        try:
            core.record_failure(exc)
        except Exception as failure_exc:
            print(f"failure recorder also failed: {failure_exc}", file=core.sys.stderr, flush=True)
        raise
