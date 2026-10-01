#!/usr/bin/env python3
"""MediaForge YouTube live segment V7.

Continuity-first handoff:
- GitHub control-plane failures never stop a healthy encoder.
- Control polling is throttled to avoid installation rate-limit exhaustion.
- Successors are prewarmed early.
- When YouTube exposes backup ingestion, adjacent runners alternate primary/backup
  endpoints and overlap until the successor proves its encoder is healthy.
- Without backup ingestion, takeover is scheduled in the future so all GitHub
  coordination completes before the current encoder disconnects.
- After cutover, no GitHub read/write is allowed to sit on the encoder critical path.
"""
import time

import mediaforge_live_segment_v6 as v6

core = v6.core

_orig_create_or_resume = core.create_or_resume_youtube

_stop_cache = {"checked": 0.0, "value": False}
_ready_cache = {"checked": 0.0, "value": False}
_backup_available = None
_endpoint_role = "primary"
_takeover_state_cache = None


def _safe_get(path):
    try:
        return core.github_get_json(path)
    except Exception as exc:
        print(f"::warning::Control-plane read ignored for {path}: {exc}", flush=True)
        return None


def _safe_put(path, payload, message):
    try:
        core.github_put_json(path, payload, message)
        return True
    except Exception as exc:
        print(f"::warning::Control-plane write deferred for {path}: {exc}", flush=True)
        return False


def stop_requested_v7():
    now = time.time()
    if now - _stop_cache["checked"] < 60:
        return _stop_cache["value"]
    _stop_cache["checked"] = now
    state = _safe_get(f"control/live-stop/{core.SESSION_ID}.json")
    if state is not None:
        _stop_cache["value"] = bool(state.get("stop") is True)
    return _stop_cache["value"]


def successor_ready_v7():
    now = time.time()
    if now - _ready_cache["checked"] < 15:
        return _ready_cache["value"]
    _ready_cache["checked"] = now
    state = _safe_get(f"control/live-ready/{core.SESSION_ID}-{core.SEGMENT_INDEX + 1}.json")
    if not state or state.get("ready") is not True:
        _ready_cache["value"] = False
        return False
    if int(state.get("segment_index") or 0) != core.SEGMENT_INDEX + 1:
        _ready_cache["value"] = False
        return False
    run_id = state.get("run_id")
    if run_id:
        status, _ = core.github_run_state(run_id)
        if status is not None:
            _ready_cache["value"] = status in {"queued", "in_progress"}
            return _ready_cache["value"]
        # Ready marker is already durable. If Actions status cannot be read, fail open
        # while the predecessor still remains online until successor proof/cutover.
        print("::warning::Successor run-state unavailable; accepting durable ready marker.", flush=True)
    _ready_cache["value"] = True
    return True


def create_or_resume_youtube_v7(thumb):
    global _backup_available, _endpoint_role
    yt, bid, sid, primary_rtmp, session_started = _orig_create_or_resume(thumb)
    try:
        stream = yt.liveStreams().list(part="cdn", id=sid).execute()["items"][0]
        info = (stream.get("cdn") or {}).get("ingestionInfo") or {}
        primary = (info.get("ingestionAddress") or "").rstrip("/")
        backup = (info.get("backupIngestionAddress") or "").rstrip("/")
        name = info.get("streamName") or ""
        _backup_available = bool(backup and name)
        if _backup_available and core.SEGMENT_INDEX % 2 == 0:
            _endpoint_role = "backup"
            chosen = backup + "/" + name
        else:
            _endpoint_role = "primary"
            chosen = primary + "/" + name if primary and name else primary_rtmp
        print(f"V7 ingest role={_endpoint_role}; backup_ingest_available={bool(_backup_available)}", flush=True)
        return yt, bid, sid, chosen, session_started
    except Exception as exc:
        _backup_available = False
        _endpoint_role = "primary"
        print(f"::warning::Could not select alternate YouTube ingest; using primary: {exc}", flush=True)
        return yt, bid, sid, primary_rtmp, session_started


def wait_takeover_v7():
    global _takeover_state_cache
    if core.SEGMENT_INDEX <= 1:
        return
    path = f"control/live-takeover/{core.SESSION_ID}-{core.SEGMENT_INDEX}.json"
    print(f"Segment {core.SEGMENT_INDEX} prewarmed; waiting for V7 takeover signal.", flush=True)
    while True:
        state = _safe_get(path)
        if state and state.get("takeover") is True and int(state.get("from_segment_index") or 0) == core.SEGMENT_INDEX - 1:
            _takeover_state_cache = dict(state)
            mode = state.get("handoff_mode") or "legacy"
            cutover = float(state.get("cutover_epoch") or 0.0)
            if mode == "scheduled-single-ingest" and cutover > time.time():
                print(f"Scheduled cutover armed; waiting locally until {cutover:.3f}.", flush=True)
                while time.time() < cutover:
                    time.sleep(min(0.05, max(0.005, cutover - time.time())))
            else:
                print(f"Takeover signal received; mode={mode}.", flush=True)
            return
        time.sleep(10)


def visual_offset_v7(loop_path, session_started):
    """Compute visual resume locally from the takeover state already cached before cutover."""
    pos = None
    if _takeover_state_cache:
        try:
            pos = float(_takeover_state_cache.get("visual_position_seconds"))
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
    print(f"V7 visual position={pos:.3f}s loop={dur:.3f}s offset={off:.3f}s", flush=True)
    return off


def verify_live_v7(yt, bid, sid):
    """Verify YouTube best-effort without ever killing a healthy post-cutover encoder for control-plane errors."""
    time.sleep(12)
    core.assert_processes()

    ingest_active = False
    for attempt in range(1, 25):
        core.assert_processes()
        try:
            items = yt.liveStreams().list(part="status", id=sid).execute().get("items") or []
            st = (items[0].get("status") or {}).get("streamStatus") if items else None
            print(f"streamStatus={st} attempt={attempt}/24", flush=True)
            if st == "active":
                ingest_active = True
                break
        except Exception as exc:
            print(f"::warning::YouTube stream-status check {attempt}/24 failed: {exc}", flush=True)
        time.sleep(5)

    lifecycle = None
    for attempt in range(1, 13):
        core.assert_processes()
        try:
            items = yt.liveBroadcasts().list(part="status", id=bid).execute().get("items") or []
            lifecycle = (items[0].get("status") or {}).get("lifeCycleStatus") if items else None
            print(f"lifeCycleStatus={lifecycle} attempt={attempt}/12", flush=True)
            if lifecycle == "live":
                break
            if lifecycle in {"complete", "revoked"}:
                raise RuntimeError(f"Broadcast is no longer resumable; lifecycle={lifecycle}")
            try:
                yt.liveBroadcasts().transition(part="status", id=bid, broadcastStatus="live").execute()
            except Exception as exc:
                print(f"transition live retry: {exc}", flush=True)
        except RuntimeError:
            raise
        except Exception as exc:
            print(f"::warning::YouTube lifecycle check {attempt}/12 failed: {exc}", flush=True)
        time.sleep(5)

    # A healthy FFmpeg process must be preserved even when YouTube's API is temporarily unavailable.
    core.assert_processes()
    now = core.iso_now()
    result = _safe_get(f"control/live-results/{core.SESSION_ID}.json") or {}
    result.update({
        "platform": "youtube",
        "status": "live" if lifecycle == "live" else "starting",
        "github_run_id": core.RUN_ID,
        "github_run_url": core.RUN_URL,
        "youtube_broadcast_id": bid,
        "youtube_stream_id": sid,
        "youtube_url": f"https://www.youtube.com/watch?v={bid}",
        "segment_index": core.SEGMENT_INDEX,
        "encoder_resolution": "1920x1080",
        "encoder_fps": 60,
        "encoder_bitrate_kbps": 8000,
        "encoder_connected": True,
        "current_segment_started_at": now,
        "last_verified_lifecycle": lifecycle,
        "youtube_api_verification_uncertain": lifecycle != "live",
    })
    result.pop("prewarmed_segment_index", None)
    result.pop("prewarmed_run_id", None)
    result.pop("prewarmed_at", None)
    if lifecycle == "live":
        result.setdefault("live_at", now)
    _safe_put(
        f"control/live-results/{core.SESSION_ID}.json",
        result,
        f"office-music: V7 live {core.SESSION_ID} segment {core.SEGMENT_INDEX}",
    )

    # For overlap handoff the important proof is: successor FFmpeg survived startup.
    # The predecessor stays online until this marker is durable.
    payload = {
        "session_id": core.SESSION_ID,
        "segment_index": core.SEGMENT_INDEX,
        "run_id": core.RUN_ID,
        "active": True,
        "ingest_role": _endpoint_role,
        "ingest_api_active": ingest_active,
        "verified_at": now,
    }
    path = f"control/live-active/{core.SESSION_ID}-{core.SEGMENT_INDEX}.json"
    for attempt in range(1, 4):
        if _safe_put(path, payload, f"live: active {core.SESSION_ID} {core.SEGMENT_INDEX}"):
            print(f"Successor-active marker published on attempt {attempt}.", flush=True)
            return
        time.sleep(10)
    print("::warning::Could not publish successor-active marker; encoder remains online and predecessor must not disconnect.", flush=True)


def _handoff_state(cutover_epoch, session_started, mode):
    state = _safe_get(f"control/live-now-playing/{core.SESSION_ID}.json") or {}
    started = core.parse_ts(state.get("started_at"))
    if started is not None:
        position = max(0.0, cutover_epoch - started)
    else:
        position = float(state.get("resume_offset_seconds") or 0.0)
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
        "handoff_protocol": "youtube-v7-continuity-first",
        "signaled_at": core.iso_now(),
        "source_run_id": core.RUN_ID,
    }


def _backup_supported(yt, sid):
    global _backup_available
    if _backup_available is not None:
        return _backup_available
    try:
        stream = yt.liveStreams().list(part="cdn", id=sid).execute()["items"][0]
        info = (stream.get("cdn") or {}).get("ingestionInfo") or {}
        _backup_available = bool(info.get("backupIngestionAddress"))
    except Exception:
        _backup_available = False
    return _backup_available


def _wait_successor_active(seconds=240):
    path = f"control/live-active/{core.SESSION_ID}-{core.SEGMENT_INDEX + 1}.json"
    deadline = time.time() + seconds
    while time.time() < deadline:
        core.assert_processes()
        state = _safe_get(path)
        if state and state.get("active") is True and int(state.get("segment_index") or 0) == core.SEGMENT_INDEX + 1:
            return True
        time.sleep(10)
    return False


def run_segment_v7(yt, bid, sid, session_started):
    chain = core.should_chain()
    seg_seconds = core.segment_seconds()
    lead = min(max(900, core.PREWARM_LEAD_SECONDS), max(30, seg_seconds // 2))
    start_epoch = int(time.time())
    deadline = start_epoch + seg_seconds
    prewarm_at = deadline - lead
    prewarm_sent = False
    last_redispatch = 0
    takeover_sent = False
    reason = "segment_complete"

    while True:
        core.assert_processes()
        now = int(time.time())

        if stop_requested_v7():
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

            if successor_ready_v7():
                if _backup_supported(yt, sid):
                    if not takeover_sent:
                        handoff = _handoff_state(time.time(), session_started, "dual-ingest-overlap")
                        path = f"control/live-takeover/{core.SESSION_ID}-{core.SEGMENT_INDEX + 1}.json"
                        if _safe_put(path, handoff, f"live: V7 overlap takeover {core.SESSION_ID} {core.SEGMENT_INDEX + 1}"):
                            takeover_sent = True
                            print("V7 takeover released while predecessor is still streaming; waiting for successor proof.", flush=True)
                    if takeover_sent and _wait_successor_active(240):
                        print("Successor encoder verified on alternate ingest. Stopping predecessor now.", flush=True)
                        core.stop_processes()
                        _safe_put(
                            f"control/live-handoffs/{core.SESSION_ID}.json",
                            _handoff_state(time.time(), session_started, "dual-ingest-overlap"),
                            f"live: V7 handoff audit {core.SESSION_ID} {core.SEGMENT_INDEX}",
                        )
                        return True, reason
                    print("Successor proof not available; predecessor remains online.", flush=True)
                else:
                    cutover = time.time() + 30.0
                    handoff = _handoff_state(cutover, session_started, "scheduled-single-ingest")
                    path = f"control/live-takeover/{core.SESSION_ID}-{core.SEGMENT_INDEX + 1}.json"
                    if _safe_put(path, handoff, f"live: V7 scheduled takeover {core.SESSION_ID} {core.SEGMENT_INDEX + 1}"):
                        print(f"Scheduled single-ingest cutover armed for {cutover:.3f}; all remote coordination is complete.", flush=True)
                        while time.time() < cutover:
                            core.assert_processes()
                            time.sleep(min(0.05, max(0.005, cutover - time.time())))
                        core.stop_processes()
                        return True, reason
                    print("Takeover coordination failed; predecessor remains online.", flush=True)
            else:
                if now - last_redispatch >= 120:
                    core.dispatch_next(bid, sid)
                    last_redispatch = now
                print("Successor not ready; current encoder remains online.", flush=True)

        time.sleep(5)

    core.stop_processes()
    return False, reason


core.create_or_resume_youtube = create_or_resume_youtube_v7
core.stop_requested = stop_requested_v7
core.successor_ready = successor_ready_v7
core.wait_takeover = wait_takeover_v7
core.visual_offset = visual_offset_v7
core.verify_live = verify_live_v7
core.run_segment = run_segment_v7


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
