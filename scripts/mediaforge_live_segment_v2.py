#!/usr/bin/env python3
import time

from googleapiclient.errors import HttpError

import mediaforge_live_segment as core


def is_quota_error(exc):
    text = str(exc)
    return isinstance(exc, HttpError) and exc.resp.status == 403 and "quotaExceeded" in text


def verify_live_quota_safe(yt, bid, sid):
    """Verify ingest/live without ever killing a healthy encoder only because API quota ran out."""
    time.sleep(12)
    core.assert_processes()

    active = False
    ingest_quota_limited = False
    for _ in range(48):
        try:
            items = yt.liveStreams().list(part="status", id=sid).execute().get("items") or []
            st = (items[0].get("status") or {}).get("streamStatus") if items else None
            print("streamStatus=", st, flush=True)
            if st == "active":
                active = True
                break
        except HttpError as exc:
            if is_quota_error(exc):
                ingest_quota_limited = True
                print("::warning::YouTube API quota exhausted while checking ingest; encoder is healthy, preserving RTMP instead of terminating it.", flush=True)
                break
            raise
        time.sleep(5)

    if not active and not ingest_quota_limited:
        raise RuntimeError("YouTube ingest did not become active")

    lifecycle = None
    lifecycle_quota_limited = False
    for _ in range(24):
        try:
            items = yt.liveBroadcasts().list(part="status", id=bid).execute().get("items") or []
            lifecycle = (items[0].get("status") or {}).get("lifeCycleStatus") if items else None
            print("lifeCycleStatus=", lifecycle, flush=True)
            if lifecycle == "live":
                # Important: once LIVE is confirmed, DO NOT query again.
                break
            try:
                yt.liveBroadcasts().transition(part="status", id=bid, broadcastStatus="live").execute()
            except HttpError as exc:
                if is_quota_error(exc):
                    lifecycle_quota_limited = True
                    print("::warning::YouTube API quota exhausted during LIVE transition check; AutoStart is enabled, preserving encoder.", flush=True)
                    break
                print("transition live retry:", exc, flush=True)
        except HttpError as exc:
            if is_quota_error(exc):
                lifecycle_quota_limited = True
                print("::warning::YouTube API quota exhausted while verifying lifecycle; preserving active encoder.", flush=True)
                break
            raise
        time.sleep(5)

    if lifecycle != "live" and not lifecycle_quota_limited:
        raise RuntimeError(f"Broadcast did not reach LIVE; lifecycle={lifecycle}")

    result = core.github_get_json(f"control/live-results/{core.SESSION_ID}.json") or {}
    now = core.iso_now()
    verified = lifecycle == "live"
    result.update({
        "platform": "youtube",
        "status": "live",
        "github_run_id": core.RUN_ID,
        "github_run_url": core.RUN_URL,
        "youtube_broadcast_id": bid,
        "youtube_stream_id": sid,
        "youtube_url": f"https://www.youtube.com/watch?v={bid}",
        "segment_index": core.SEGMENT_INDEX,
        "encoder_resolution": "1920x1080",
        "encoder_fps": 60,
        "encoder_bitrate_kbps": 8000,
        "current_segment_started_at": now,
        "last_verified_lifecycle": "live" if verified else "api_quota_unavailable_encoder_preserved",
        "youtube_api_quota_limited": bool(ingest_quota_limited or lifecycle_quota_limited),
    })
    result.pop("prewarmed_segment_index", None)
    result.pop("prewarmed_run_id", None)
    result.pop("prewarmed_at", None)
    result.setdefault("live_at", now)
    core.github_put_json(
        f"control/live-results/{core.SESSION_ID}.json",
        result,
        f"office-music: live {core.SESSION_ID} segment {core.SEGMENT_INDEX}",
    )


core.verify_live = verify_live_quota_safe


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
