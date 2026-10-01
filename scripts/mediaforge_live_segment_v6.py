#!/usr/bin/env python3
"""MediaForge YouTube live segment V6.

V6 keeps the V5 API/SSL resilience and replaces the live audio path with a
jitter-resistant PCM pipeline. The feeder decodes ahead of realtime, while the
outer encoder owns pacing and keeps a much larger audio queue.
"""
import subprocess
import sys

import mediaforge_live_segment_v5 as v5

core = v5.core


def start_encoder_smooth(loop_path, rtmp, vis_offset):
    global core
    core.feeder_log = open(core.BUILD / "audio-feeder.log", "wb")
    core.encoder_log = open(core.BUILD / "ffmpeg.log", "wb")

    core.feeder = subprocess.Popen(
        [
            sys.executable, "scripts/mediaforge_live_audio_feeder.py",
            "--session-id", core.SESSION_ID,
            "--fallback-b64", core.TRACK_URLS_B64,
            "--segment-index", str(core.SEGMENT_INDEX),
        ],
        stdout=subprocess.PIPE,
        stderr=core.feeder_log,
        bufsize=0,
    )

    video_args = ["-re", "-stream_loop", "-1"]
    if vis_offset > 0.05:
        video_args += ["-ss", f"{vis_offset:.3f}"]
    video_args += ["-i", str(loop_path)]

    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "warning",
        *video_args,
        "-thread_queue_size", "8192",
        "-f", "s16le", "-ar", "48000", "-ac", "2", "-i", "pipe:0",
        "-map", "0:v:0", "-map", "1:a:0",
        "-vf", "scale=1920:1080:force_original_aspect_ratio=decrease:flags=lanczos,pad=1920:1080:(ow-iw)/2:(oh-ih)/2,setsar=1,fps=60,format=yuv420p",
        "-af", "aresample=48000:async=1000:first_pts=0",
        "-r", "60", "-s:v", "1920x1080", "-pix_fmt", "yuv420p",
        "-c:v", "libx264", "-preset", "superfast", "-threads", "0",
        "-profile:v", "high", "-level:v", "4.2",
        "-b:v", "8000k", "-minrate", "8000k", "-maxrate", "8000k", "-bufsize", "16000k",
        "-g", "120", "-keyint_min", "120", "-sc_threshold", "0",
        "-x264-params", "nal-hrd=cbr:force-cfr=1",
        "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2",
        "-max_interleave_delta", "1000000",
        "-f", "flv", rtmp,
    ]

    core.encoder = subprocess.Popen(
        cmd,
        stdin=core.feeder.stdout,
        stdout=core.encoder_log,
        stderr=core.encoder_log,
        bufsize=0,
    )
    core.feeder.stdout.close()
    core.took_over = True
    print("V6 smooth-audio encoder started: feeder prefetch + 8192 packet queue + async resample.", flush=True)


core.start_encoder = start_encoder_smooth


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
