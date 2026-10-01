#!/usr/bin/env python3
"""MediaForge YouTube live segment V5.

V5 keeps a healthy RTMP encoder alive when YouTube Data API verification suffers
transient SSL/network/API failures. Verification is observability, not a reason
to tear down working ingest.
"""
import ssl
import time

from googleapiclient.errors import HttpError

import mediaforge_live_segment_v4 as v4

core = v4.core


def is_quota_error(exc):
    text = str(exc)
    return isinstance(exc, HttpError) and exc.resp.status == 403 and "quotaExceeded" in text


def clear_http_connections(yt):
    try:
        http = getattr(yt, "_http", None)
        conns = getattr(http, "connections", None)
        if conns is not None:
            conns.clear()
    except Exception:
        pass


def transient_api_error(exc):
    if isinstance(exc, HttpError):
        status = int(getattr(exc.resp, "status", 0) or 0)
        return status == 429 or status >= 500
    return isinstance(exc, (ssl.SSLError, OSError, TimeoutError, ConnectionError))


def verify_live_resilient(yt, bid, sid):
    """Verify YouTube without ever killing healthy RTMP due to API transport trouble."""
    time.sleep(12)
    core.assert_processes()

    active = False
    verification_uncertain = False
    last_error = None

    # Ingest verification. A transient API failure must never terminate FFmpeg.
    for attempt in range(1, 49):
        core.assert_processes()
        try:
            items = yt.liveStreams().list(part="status", id=sid).execute().get("items") or []
            state = (items[0].get("status") or {}).get("streamStatus") if items else None
            print(f"streamStatus={state} attempt={attempt}/48", flush=True)
            if state == "active":
                active = True
                break
        except HttpError as exc:
            last_error = exc
            if is_quota_error(exc):
                verification_uncertain = True
                print("::warning::YouTube API quota unavailable while checking ingest; preserving healthy RTMP encoder.", flush=True)
                break
            if transient_api_error(exc):
                print(f"::warning::Transient YouTube API error while checking ingest ({attempt}/48): {exc}", flush=True)
                clear_http_connections(yt)
            else:
                # Verification is non-destructive: even an unexpected API response should
                # not kill an encoder that is still connected and producing media.
                verification_uncertain = True
                print(f"::warning::YouTube ingest verification error; preserving encoder: {exc}", flush=True)
                break
        except Exception as exc:
            last_error = exc
            verification_uncertain = True
            print(f"::warning::YouTube transport/SSL error while checking ingest ({attempt}/48): {exc}", flush=True)
            clear_http_connections(yt)
        time.sleep(5)

    if not active:
        verification_uncertain = True
        print("::warning::Ingest could not be positively verified; encoder is healthy, so it will remain online.", flush=True)

    lifecycle = None
    verified_live = False

    # Lifecycle verification/transition. Again, API availability must not own encoder lifetime.
    for attempt in range(1, 25):
        core.assert_processes()
        try:
            items = yt.liveBroadcasts().list(part="status", id=bid).execute().get("items") or []
            lifecycle = (items[0].get("status") or {}).get("lifeCycleStatus") if items else None
            print(f"lifeCycleStatus={lifecycle} attempt={attempt}/24", flush=True)
            if lifecycle == "live":
                verified_live = True
                break
            if lifecycle in {"complete", "revoked"}:
                # The current broadcast cannot transition back to LIVE, but keeping ingest
                # alive is still safer than killing it here; monitoring/recovery can act.
                verification_uncertain = True
                print(f"::warning::Broadcast lifecycle is {lifecycle}; preserving encoder for recovery logic.", flush=True)
                break
            try:
                yt.liveBroadcasts().transition(part="status", id=bid, broadcastStatus="live").execute()
            except HttpError as exc:
                last_error = exc
                if is_quota_error(exc):
                    verification_uncertain = True
                    print("::warning::YouTube API quota unavailable during LIVE transition; AutoStart/encoder remain active.", flush=True)
                    break
                if transient_api_error(exc):
                    print(f"::warning::Transient LIVE transition API error ({attempt}/24): {exc}", flush=True)
                    clear_http_connections(yt)
                else:
                    print(f"LIVE transition retry: {exc}", flush=True)
            except Exception as exc:
                last_error = exc
                verification_uncertain = True
                print(f"::warning::YouTube transport/SSL error during LIVE transition ({attempt}/24): {exc}", flush=True)
                clear_http_connections(yt)
        except HttpError as exc:
            last_error = exc
            if is_quota_error(exc):
                verification_uncertain = True
                print("::warning::YouTube API quota unavailable while verifying lifecycle; preserving encoder.", flush=True)
                break
            if transient_api_error(exc):
                print(f"::warning::Transient lifecycle API error ({attempt}/24): {exc}", flush=True)
                clear_http_connections(yt)
            else:
                verification_uncertain = True
                print(f"::warning::Lifecycle verification error; preserving encoder: {exc}", flush=True)
                break
        except Exception as exc:
            last_error = exc
            verification_uncertain = True
            print(f"::warning::YouTube transport/SSL error while verifying lifecycle ({attempt}/24): {exc}", flush=True)
            clear_http_connections(yt)
        time.sleep(5)

    result = core.github_get_json(f"control/live-results/{core.SESSION_ID}.json") or {}
    now = core.iso_now()
    result.update({
        "platform": "youtube",
        "status": "live" if verified_live else "starting",
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
        "last_verified_lifecycle": lifecycle or ("api_unavailable_encoder_preserved" if verification_uncertain else "unknown"),
        "youtube_api_verification_uncertain": bool(verification_uncertain and not verified_live),
    })
    if last_error and not verified_live:
        result["verification_warning"] = str(last_error)[:500]
    else:
        result.pop("verification_warning", None)
    for key in ("prewarmed_segment_index", "prewarmed_run_id", "prewarmed_at", "error_message", "completed_at", "failed_at"):
        result.pop(key, None)
    if verified_live:
        result.setdefault("live_at", now)
    core.github_put_json(
        f"control/live-results/{core.SESSION_ID}.json",
        result,
        f"office-music: encoder preserved {core.SESSION_ID} segment {core.SEGMENT_INDEX}",
    )


core.verify_live = verify_live_resilient


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
