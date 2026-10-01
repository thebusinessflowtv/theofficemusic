#!/usr/bin/env python3
"""MediaForge Kick segment V3 — continuity-first scheduled cutover.

A single Kick stream key cannot safely accept two intentional publishers at once.
V3 therefore prepares the successor early, commits a future cutover timestamp while
the predecessor is still streaming, and lets both runners switch using local clocks.
GitHub control-plane failures are non-fatal to a healthy encoder.
"""
import time

import mediaforge_kick_segment_v2 as v2

_stop_cache = {"checked": 0.0, "value": False}
_ready_cache = {"checked": 0.0, "value": False}


def safe_get(path):
    try:
        return v2.github_get_json(path)
    except Exception as exc:
        print(f"::warning::Kick control-plane read ignored for {path}: {exc}", flush=True)
        return None


def safe_put(path, payload, message):
    try:
        v2.github_put_json(path, payload, message)
        return True
    except Exception as exc:
        print(f"::warning::Kick control-plane write deferred for {path}: {exc}", flush=True)
        return False


def stop_requested_v3():
    now = time.time()
    if now - _stop_cache["checked"] < 60:
        return _stop_cache["value"]
    _stop_cache["checked"] = now
    state = safe_get(f"control/kick-live-stop/{v2.SESSION_ID}.json")
    if state is not None:
        _stop_cache["value"] = bool(state.get("stop") is True)
    return _stop_cache["value"]


def successor_ready_v3():
    now = time.time()
    if now - _ready_cache["checked"] < 15:
        return _ready_cache["value"]
    _ready_cache["checked"] = now
    state = safe_get(f"control/kick-prewarm/{v2.SESSION_ID}-{v2.SEGMENT_INDEX + 1}.json")
    if not state or state.get("ready") is not True:
        _ready_cache["value"] = False
        return False
    run_id = state.get("run_id")
    if not run_id:
        _ready_cache["value"] = True
        return True
    try:
        run = v2.api_request("GET", f"actions/runs/{int(run_id)}")
        _ready_cache["value"] = run.get("status") in {"queued", "in_progress"}
    except Exception as exc:
        print(f"::warning::Kick successor run-state unavailable; ready marker accepted: {exc}", flush=True)
        _ready_cache["value"] = True
    return _ready_cache["value"]


def wait_takeover_v3():
    if v2.SEGMENT_INDEX <= 1:
        return
    path = f"control/live-takeover/{v2.SESSION_ID}-{v2.SEGMENT_INDEX}.json"
    started = time.time()
    offline_streak = 0
    print(f"Kick segment {v2.SEGMENT_INDEX} armed; waiting for scheduled cutover.", flush=True)
    while True:
        state = safe_get(path)
        if state and state.get("takeover") is True and int(state.get("from_segment_index") or 0) == v2.SEGMENT_INDEX - 1:
            cutover = float(state.get("cutover_epoch") or 0.0)
            if cutover > time.time():
                print(f"Kick cutover timestamp received; waiting locally until {cutover:.3f}.", flush=True)
                while time.time() < cutover:
                    time.sleep(min(0.05, max(0.005, cutover - time.time())))
            else:
                print("Kick takeover signal received without future timestamp; taking over immediately.", flush=True)
            return

        # Emergency path only: if predecessor died without being able to signal, recover.
        if time.time() - started >= 30:
            ks = v2.kick_state()
            offline_streak = offline_streak + 1 if ks == "offline" else 0
            if offline_streak >= 3:
                print("Kick predecessor confirmed offline; emergency takeover.", flush=True)
                return
        time.sleep(10)


def capture_scheduled_handoff(cutover_epoch):
    state = safe_get(f"control/live-now-playing/{v2.SESSION_ID}.json") or {}
    started = v2.parse_ts(state.get("started_at"))
    position = max(0.0, cutover_epoch - started) if started is not None else float(state.get("resume_offset_seconds") or 0)
    return {
        "platform": "kick",
        "session_id": v2.SESSION_ID,
        "segment_index": v2.SEGMENT_INDEX + 1,
        "from_segment_index": v2.SEGMENT_INDEX,
        "to_segment_index": v2.SEGMENT_INDEX + 1,
        "takeover": True,
        "track_id": state.get("track_id"),
        "title": state.get("title"),
        "url": state.get("url"),
        "track_started_at": state.get("started_at"),
        "position_seconds": round(position, 3),
        "cutover_epoch": round(cutover_epoch, 6),
        "signaled_at": v2.iso_now(),
        "source_run_id": v2.RUN_ID,
        "handoff_mode": "scheduled-single-key",
        "handoff_protocol": "kick-v3-continuity-first",
    }


def run_segment_v3(loop):
    chain = v2.DURATION_MINUTES == 0 or v2.DURATION_MINUTES > 300
    seconds = 300 * 60 if v2.DURATION_MINUTES == 0 else min(300, v2.DURATION_MINUTES) * 60
    deadline = int(time.time()) + seconds
    # 15-minute runway instead of 10 minutes.
    prewarm_at = deadline - min(900, max(30, seconds // 2))
    dispatched = False
    last_dispatch = 0
    reconnects = 0

    while True:
        now = int(time.time())
        if stop_requested_v3():
            v2.stop_processes()
            return False

        if not v2.encoder or v2.encoder.poll() is not None or not v2.feeder or v2.feeder.poll() is not None:
            reconnects += 1
            if reconnects > 8:
                raise RuntimeError("Kick reconnect limit reached")
            print(f"Kick reconnect {reconnects}/8.", flush=True)
            v2.start_encoder(loop)
            time.sleep(12)

        if chain and not dispatched and now >= prewarm_at:
            dispatched = v2.dispatch_next()
            last_dispatch = now

        if now >= deadline:
            if not chain:
                v2.stop_processes()
                return False
            if not dispatched:
                dispatched = v2.dispatch_next()
                last_dispatch = now

            if successor_ready_v3():
                # Critical invariant: publish the takeover plan before disconnecting.
                cutover = time.time() + 30.0
                handoff = capture_scheduled_handoff(cutover)
                path = f"control/live-takeover/{v2.SESSION_ID}-{v2.SEGMENT_INDEX + 1}.json"
                if safe_put(path, handoff, f"peter-lofi: Kick V3 scheduled takeover {v2.SESSION_ID} {v2.SEGMENT_INDEX + 1}"):
                    print(f"Kick V3 cutover fully coordinated for {cutover:.3f}; predecessor remains live until that instant.", flush=True)
                    while time.time() < cutover:
                        v2.assert_processes()
                        time.sleep(min(0.05, max(0.005, cutover - time.time())))
                    # No GitHub/API request is required after this point.
                    v2.stop_processes()
                    return True
                print("Kick takeover coordination failed; current encoder remains online.", flush=True)
            else:
                if now - last_dispatch >= 120:
                    v2.dispatch_next()
                    last_dispatch = now
                print("Kick successor not ready; current encoder remains online.", flush=True)

        time.sleep(5)


v2.stop_requested = stop_requested_v3
v2.successor_ready = successor_ready_v3
v2.wait_takeover = wait_takeover_v3
v2.capture_handoff = capture_scheduled_handoff
v2.run_segment = run_segment_v3


if __name__ == "__main__":
    try:
        v2.main()
    except Exception as exc:
        print(f"FATAL: {exc}", file=v2.sys.stderr, flush=True)
        try:
            v2.stop_processes()
        except Exception:
            pass
        v2.record_failure(exc)
        raise
