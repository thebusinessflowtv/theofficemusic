#!/usr/bin/env python3
import base64
import hashlib
import json
import time

from cryptography.fernet import Fernet, InvalidToken
from googleapiclient.errors import HttpError

import mediaforge_live_segment as core


def is_quota_error(exc):
    text = str(exc)
    return isinstance(exc, HttpError) and exc.resp.status == 403 and "quotaExceeded" in text


def fernet():
    # Derive an encryption key exclusively from GitHub Actions secrets. The stored token
    # can safely live in the public repository; it is useless without these secrets.
    material = (
        core.YOUTUBE_CLIENT_SECRET + "\0" + core.YOUTUBE_REFRESH_TOKEN + "\0" + core.YOUTUBE_CHANNEL_ID
    ).encode("utf-8")
    key = base64.urlsafe_b64encode(hashlib.sha256(material).digest())
    return Fernet(key)


def seal_rtmp(rtmp):
    return fernet().encrypt(rtmp.encode("utf-8")).decode("ascii")


def open_rtmp(token):
    try:
        return fernet().decrypt(token.encode("ascii"), ttl=None).decode("utf-8")
    except (InvalidToken, ValueError) as exc:
        raise RuntimeError("Encrypted RTMP handoff token is invalid") from exc


_original_create_or_resume = core.create_or_resume_youtube


def create_or_resume_without_api_dependency(thumb):
    """Resume established streams from encrypted state without spending YouTube API quota."""
    existing = core.github_get_json(f"control/live-results/{core.SESSION_ID}.json") or {}
    bid = core.RESUME_BROADCAST_ID
    sid = core.RESUME_STREAM_ID
    token = existing.get("rtmp_handoff_token")

    if bid and sid and token:
        rtmp = open_rtmp(token)
        yt = core.youtube_client()  # creates the client object but performs no request
        session_started = existing.get("session_started_at") or existing.get("started_at") or core.iso_now()
        result = dict(existing)
        result.update({
            "platform": "youtube",
            "youtube_broadcast_id": bid,
            "youtube_stream_id": sid,
            "youtube_url": f"https://www.youtube.com/watch?v={bid}",
            "session_started_at": session_started,
            "prewarmed_segment_index": core.SEGMENT_INDEX,
            "prewarmed_run_id": core.RUN_ID,
            "prewarmed_at": core.iso_now(),
            "rtmp_resume_mode": "encrypted_handoff_token",
        })
        core.github_put_json(
            f"control/live-results/{core.SESSION_ID}.json",
            result,
            f"office-music: segment prepared offline {core.SESSION_ID} {core.SEGMENT_INDEX}",
        )
        print("Prepared resumed segment from encrypted RTMP handoff token; no YouTube API lookup required.", flush=True)
        return yt, bid, sid, rtmp, session_started

    # First segment, or first migration from the legacy workflow. Use the API once,
    # then persist only an encrypted RTMP token for all future runner handoffs.
    yt, bid, sid, rtmp, session_started = _original_create_or_resume(thumb)
    result = core.github_get_json(f"control/live-results/{core.SESSION_ID}.json") or {}
    result["rtmp_handoff_token"] = seal_rtmp(rtmp)
    result["rtmp_resume_mode"] = "encrypted_handoff_token_ready"
    core.github_put_json(
        f"control/live-results/{core.SESSION_ID}.json",
        result,
        f"office-music: secure RTMP handoff state {core.SESSION_ID}",
    )
    return yt, bid, sid, rtmp, session_started


def verify_live_quota_safe(yt, bid, sid):
    """Never kill a healthy RTMP encoder merely because YouTube Data API quota is unavailable."""
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
                print("::warning::YouTube API quota unavailable while checking ingest; preserving healthy encoder.", flush=True)
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
                break
            try:
                yt.liveBroadcasts().transition(part="status", id=bid, broadcastStatus="live").execute()
            except HttpError as exc:
                if is_quota_error(exc):
                    lifecycle_quota_limited = True
                    print("::warning::YouTube API quota unavailable during transition check; AutoStart remains enabled and encoder stays online.", flush=True)
                    break
                print("transition live retry:", exc, flush=True)
        except HttpError as exc:
            if is_quota_error(exc):
                lifecycle_quota_limited = True
                print("::warning::YouTube API quota unavailable while verifying lifecycle; preserving encoder.", flush=True)
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
    # Never leave stale failure/completion metadata after a recovered segment.
    for key in ("prewarmed_segment_index", "prewarmed_run_id", "prewarmed_at", "error_message", "completed_at", "failed_at"):
        result.pop(key, None)
    result.setdefault("live_at", now)
    core.github_put_json(
        f"control/live-results/{core.SESSION_ID}.json",
        result,
        f"office-music: live {core.SESSION_ID} segment {core.SEGMENT_INDEX}",
    )


core.create_or_resume_youtube = create_or_resume_without_api_dependency
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
