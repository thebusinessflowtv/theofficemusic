#!/usr/bin/env python3
import argparse
import json
import os
import pathlib
import signal
import subprocess
import sys
import time
import urllib.parse
from datetime import datetime, timezone


def iso_now():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def atomic_json(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def read_json(path, default=None):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


class StreamCore:
    def __init__(self, platform):
        self.platform = platform
        self.state = pathlib.Path("/state") / platform
        self.state.mkdir(parents=True, exist_ok=True)
        self.health_path = self.state / "health.json"
        self.desired_path = self.state / "desired.json"
        self.runtime_playlist = self.state / "playlist.json"
        self.base_playlist = os.environ.get("PLAYLIST_FILE", "/config/gaming.json")
        self.stream_url = os.environ.get("STREAM_URL", "").rstrip("/")
        self.stream_key = os.environ.get("STREAM_KEY", "").strip()
        self.base_loop_url = os.environ.get("LOOP_URL", "").strip()
        self.fps = int(os.environ.get("VIDEO_FPS", "60" if platform.startswith("youtube") else "30"))
        self.vbitrate = int(os.environ.get("VIDEO_BITRATE_KBPS", "8000" if platform.startswith("youtube") else "6000"))
        self.bufsize = int(os.environ.get("VIDEO_BUFSIZE_KBPS", str(self.vbitrate * 2)))
        self.abitrate = int(os.environ.get("AUDIO_BITRATE_KBPS", "160"))
        self.vprofile = os.environ.get("VIDEO_PROFILE", "main").strip() or "main"
        self.vpreset = os.environ.get("VIDEO_PRESET", "superfast").strip() or "superfast"
        self.profile_version = os.environ.get("STREAM_PROFILE_VERSION", "stable-start-v2").strip() or "stable-start-v2"
        self.startup_preroll_seconds = max(0.25, float(os.environ.get("STARTUP_PREROLL_SECONDS", "1.5")))
        self.audio_ready_timeout = max(10.0, float(os.environ.get("AUDIO_READY_TIMEOUT_SECONDS", "90")))
        self.video_udp_port = int(os.environ.get("VIDEO_UDP_PORT", "19000"))

        self.encoder = None
        self.audio_feeder = None
        self.visual_feeder = None
        self.audio_fd = None
        self.ffmpeg_log = None
        self.running = True
        self.restarts = 0
        self.current_generation = None
        self.connected_since = None
        self.bootstrap_runtime()

    def bootstrap_runtime(self):
        if not self.runtime_playlist.exists():
            src = pathlib.Path(self.base_playlist)
            if src.exists():
                self.runtime_playlist.write_bytes(src.read_bytes())
        if not self.desired_path.exists():
            atomic_json(
                self.desired_path,
                {
                    "desired": "live",
                    "runtime": "ovh",
                    "runtime_slot": self.platform,
                    "session_id": os.environ.get("BOOTSTRAP_SESSION_ID", ""),
                    "title": os.environ.get("BOOTSTRAP_TITLE", ""),
                    "loop_url": self.base_loop_url,
                    "generation": 1,
                    "visual_revision": 1,
                    "updated_at": iso_now(),
                },
            )

    def validate(self):
        if not self.stream_url:
            raise RuntimeError("STREAM_URL is missing")
        if not self.stream_key:
            raise RuntimeError("STREAM_KEY is missing")
        if not self.base_loop_url:
            raise RuntimeError("LOOP_URL is missing")
        if not self.runtime_playlist.exists():
            raise RuntimeError(f"playlist file not found: {self.runtime_playlist}")

        # Never open RTMP with a drifted profile. If production settings are
        # wrong, fail before connecting instead of starting an unstable live.
        expected = {
            "kick": {"fps": 30, "vbitrate": 5000, "bufsize": 10000, "abitrate": 160, "profile": "main", "preset": "superfast"},
            "twitch": {"fps": 30, "vbitrate": 4500, "bufsize": 9000, "abitrate": 160, "profile": "main", "preset": "superfast"},
            "youtube-deep-house": {"fps": 60, "vbitrate": 8000, "bufsize": 16000, "abitrate": 192, "profile": "main", "preset": "superfast"},
            "youtube-rainy": {"fps": 60, "vbitrate": 8000, "bufsize": 16000, "abitrate": 192, "profile": "main", "preset": "superfast"},
            "youtube-ui-test": {"fps": 30, "vbitrate": 4500, "bufsize": 9000, "abitrate": 160, "profile": "main", "preset": "superfast"},
        }.get(self.platform)
        actual = {
            "fps": self.fps,
            "vbitrate": self.vbitrate,
            "bufsize": self.bufsize,
            "abitrate": self.abitrate,
            "profile": self.vprofile,
            "preset": self.vpreset,
        }
        if expected and actual != expected:
            raise RuntimeError(f"unsafe encoder profile for {self.platform}: expected={expected} actual={actual}")

    def desired(self):
        data = read_json(self.desired_path, {}) or {}
        data.setdefault("desired", "live")
        data.setdefault("loop_url", self.base_loop_url)
        data.setdefault("generation", 1)
        data.setdefault("visual_revision", 1)
        data.setdefault("runtime", "ovh")
        data.setdefault("runtime_slot", self.platform)
        return data

    def target(self):
        base = self.stream_url.rstrip("/")
        if self.platform == "kick":
            parsed = urllib.parse.urlparse(base)
            if parsed.scheme == "rtmps" and "global-contribute.live-video.net" in (parsed.hostname or ""):
                netloc = parsed.netloc if parsed.port is not None else f"{parsed.hostname}:443"
                path = parsed.path.rstrip("/") or "/app"
                base = urllib.parse.urlunparse((parsed.scheme, netloc, path, "", "", "")).rstrip("/")
        return f"{base}/{self.stream_key.lstrip('/')}"

    def ensure_audio_fifo(self):
        fifo = self.state / "audio.pcm"
        if not fifo.exists():
            os.mkfifo(fifo)
        if self.audio_fd is None:
            # Keep one RDWR descriptor open so the persistent encoder never sees EOF
            # while the audio engine is being recovered.
            self.audio_fd = os.open(fifo, os.O_RDWR)
        return fifo

    def stop_proc(self, proc, timeout=1.5):
        if not proc or proc.poll() is not None:
            return
        try:
            proc.send_signal(signal.SIGINT)
            proc.wait(timeout=timeout)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass

    def stop_encoder(self):
        self.stop_proc(self.encoder, timeout=2.0)
        self.encoder = None
        self.connected_since = None
        if self.ffmpeg_log is not None:
            try:
                self.ffmpeg_log.close()
            except Exception:
                pass
            self.ffmpeg_log = None

    def stop_audio(self):
        self.stop_proc(self.audio_feeder)
        self.audio_feeder = None

    def stop_visual(self):
        self.stop_proc(self.visual_feeder)
        self.visual_feeder = None

    def cleanup(self):
        self.stop_encoder()
        self.stop_audio()
        self.stop_visual()
        if self.audio_fd is not None:
            try:
                os.close(self.audio_fd)
            except Exception:
                pass
            self.audio_fd = None

    def start_audio(self):
        fifo = self.ensure_audio_fifo()
        if self.audio_feeder and self.audio_feeder.poll() is None:
            return
        self.audio_feeder = subprocess.Popen(
            [
                sys.executable,
                "/app/audio_engine.py",
                "--platform",
                self.platform,
                "--playlist-file",
                str(self.runtime_playlist),
                "--state-dir",
                "/state",
            ],
            stdout=self.audio_fd,
            stderr=subprocess.DEVNULL,
        )
        return fifo

    def start_visual(self):
        if self.visual_feeder and self.visual_feeder.poll() is None:
            return
        self.visual_feeder = subprocess.Popen(
            [
                sys.executable,
                "/app/visual_engine.py",
                "--platform",
                self.platform,
                "--state-dir",
                "/state",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    def visual_ready(self):
        vh = read_json(self.state / "visual-health.json", {}) or {}
        updated = str(vh.get("updated_at") or "")
        return vh.get("status") == "streaming" and bool(updated)

    def wait_visual_ready(self, timeout=180):
        end = time.time() + timeout
        while self.running and time.time() < end:
            if self.visual_feeder and self.visual_feeder.poll() is not None:
                raise RuntimeError(f"visual engine exited with code {self.visual_feeder.returncode}")
            if self.visual_ready():
                return
            time.sleep(0.5)
        raise RuntimeError("visual engine did not become ready")

    def audio_ready(self):
        ah = read_json(self.state / "audio-health.json", {}) or {}
        now_playing = read_json(self.state / "now-playing.json", {}) or {}
        state = str(ah.get("state") or ah.get("status") or "").lower()
        track_id = ah.get("track_id") or now_playing.get("track_id")
        return state in {
            "playing",
            "crossfading",
            "encoder_backpressure_buffering",
            "source_stalled_pcm_clock_preserved",
            "prebuffering_transition",
        } and bool(track_id)

    def wait_audio_ready(self):
        end = time.time() + self.audio_ready_timeout
        while self.running and time.time() < end:
            if self.audio_feeder and self.audio_feeder.poll() is not None:
                raise RuntimeError(f"audio engine exited with code {self.audio_feeder.returncode}")
            if self.audio_ready():
                # Pre-roll the PCM clock before opening RTMP. This stops a new
                # session from connecting while its first track is still warming.
                time.sleep(self.startup_preroll_seconds)
                return
            time.sleep(0.25)
        raise RuntimeError("audio engine did not become ready before RTMP start")

    def start_encoder(self):
        if self.encoder and self.encoder.poll() is None:
            return
        fifo = self.ensure_audio_fifo()
        self.wait_visual_ready()
        self.wait_audio_ready()
        video_input = (
            f"udp://127.0.0.1:{self.video_udp_port}"
            "?fifo_size=1000000&overrun_nonfatal=1"
        )
        common = [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "warning",
            "-nostdin",
            "-fflags",
            "+genpts+discardcorrupt",
        ]
        if self.platform.startswith("youtube"):
            common += ["-use_wallclock_as_timestamps", "1"]
        common += [
            "-thread_queue_size",
            "8192",
            "-analyzeduration",
            "3000000",
            "-probesize",
            "8000000",
            "-i",
            video_input,
            "-thread_queue_size",
            "8192",
            "-f",
            "s16le",
            "-ar",
            "48000",
            "-ac",
            "2",
            "-i",
            str(fifo),
        ]

        if self.platform in {"twitch", "kick"}:
            # Twitch/Kick use a fresh CFR A/V clock inside the publisher.
            # Audio timestamps are rebuilt from sample count so a track change
            # cannot reset the mux clock. Keep the muxer from waiting forever
            # for audio if one decoder is briefly late.
            av_cmd = common + [
                "-filter_complex",
                f"[0:v]setpts=PTS-STARTPTS,fps={self.fps},format=yuv420p[v];"
                "[1:a]asetpts=N/SR/TB,aresample=async=1000:min_hard_comp=0.100:first_pts=0[a]",
                "-map",
                "[v]",
                "-map",
                "[a]",
                "-c:v",
                "libx264",
                "-preset",
                self.vpreset,
                "-tune",
                "zerolatency",
                "-profile:v",
                self.vprofile,
                "-bf",
                "0",
                "-b:v",
                f"{self.vbitrate}k",
                "-minrate",
                f"{self.vbitrate}k",
                "-maxrate",
                f"{self.vbitrate}k",
                "-bufsize",
                f"{self.bufsize}k",
                "-g",
                str(self.fps * 2),
                "-keyint_min",
                str(self.fps * 2),
                "-sc_threshold",
                "0",
                "-x264-params",
                "nal-hrd=cbr:force-cfr=1",
                "-c:a",
                "aac",
                "-b:a",
                f"{self.abitrate}k",
                "-ar",
                "48000",
                "-ac",
                "2",
                "-max_interleave_delta",
                "250000",
                "-flush_packets",
                "1",
            ]
            if self.platform == "twitch":
                # Keep the encoder alive through short ingest/network hiccups.
                # The fifo muxer reconnects the FLV publisher instead of making
                # StreamCore tear down the whole A/V pipeline.
                cmd = av_cmd + [
                    "-f",
                    "fifo",
                    "-fifo_format",
                    "flv",
                    "-queue_size",
                    "1200",
                    "-attempt_recovery",
                    "1",
                    "-recover_any_error",
                    "1",
                    "-recovery_wait_time",
                    "1",
                    "-drop_pkts_on_overflow",
                    "1",
                    "-restart_with_keyframe",
                    "1",
                    "-max_recovery_attempts",
                    "1000000",
                    self.target(),
                ]
            else:
                # Leave the known-good Kick transport unchanged apart from the
                # safer A/V timestamp handling above.
                cmd = av_cmd + [
                    "-flvflags",
                    "no_duration_filesize",
                    "-f",
                    "flv",
                    self.target(),
                ]
        else:
            youtube_av = common + [
                "-map",
                "0:v:0",
                "-map",
                "1:a:0",
                "-c:v",
                "copy",
                "-c:a",
                "aac",
                "-b:a",
                f"{self.abitrate}k",
                "-ar",
                "48000",
                "-ac",
                "2",
                "-max_interleave_delta",
                "1000000",
            ]
            if self.platform == "youtube-ui-test":
                # Test slot: use the same simple direct FLV transport already
                # proven by the production GitHub YouTube encoder. This makes
                # connection failures visible instead of being hidden inside
                # the fifo recovery child.
                cmd = youtube_av + [
                    "-flvflags",
                    "no_duration_filesize",
                    "-f",
                    "flv",
                    self.target(),
                ]
            else:
                # Existing production YouTube slots keep their current path.
                cmd = youtube_av + [
                    "-f",
                    "fifo",
                    "-fifo_format",
                    "flv",
                    "-queue_size",
                    "1200",
                    "-attempt_recovery",
                    "1",
                    "-recover_any_error",
                    "1",
                    "-recovery_wait_time",
                    "1",
                    "-drop_pkts_on_overflow",
                    "1",
                    "-restart_with_keyframe",
                    "1",
                    "-max_recovery_attempts",
                    "1000000",
                    self.target(),
                ]
        self.ffmpeg_log = open(self.state / "ffmpeg.log", "ab", buffering=0)
        self.encoder = subprocess.Popen(cmd, stdout=self.ffmpeg_log, stderr=self.ffmpeg_log)
        self.connected_since = time.time()

    def write_health(self, status, desired, extra=None):
        visual_health = read_json(self.state / "visual-health.json", {}) or {}
        audio_health = read_json(self.state / "audio-health.json", {}) or {}
        payload = {
            "platform": self.platform,
            "runtime": "ovh",
            "runtime_slot": self.platform,
            "session_id": desired.get("session_id") or "",
            "title": desired.get("title") or "",
            "status": status,
            "fps": self.fps,
            "video_bitrate_kbps": self.vbitrate,
            "video_bufsize_kbps": self.bufsize,
            "video_profile": self.vprofile,
            "video_preset": self.vpreset,
            "stream_profile_version": self.profile_version,
            "startup_preroll_seconds": self.startup_preroll_seconds,
            "restarts": self.restarts,
            "updated_at": iso_now(),
            "loop_url": desired.get("loop_url") or self.base_loop_url,
            "visual_revision": desired.get("visual_revision") or 1,
            "visual_status": visual_health.get("status") or "unknown",
            "audio_status": audio_health.get("state") or audio_health.get("status") or "unknown",
            "audio_stalls": int(audio_health.get("stalls") or 0),
            "audio_cache_hits": int(audio_health.get("cache_hits") or 0),
            "audio_cache_misses": int(audio_health.get("cache_misses") or 0),
            "hot_swap": True,
        }
        if self.encoder and self.encoder.poll() is None:
            payload["encoder_pid"] = self.encoder.pid
        if self.audio_feeder and self.audio_feeder.poll() is None:
            payload["audio_pid"] = self.audio_feeder.pid
        if self.visual_feeder and self.visual_feeder.poll() is None:
            payload["visual_pid"] = self.visual_feeder.pid
        if extra:
            payload.update(extra)
        atomic_json(self.health_path, payload)

    def monitor(self):
        while self.running:
            desired = self.desired()
            desired_state = str(desired.get("desired") or "live").lower()
            generation = str(desired.get("generation") or "1")

            if desired_state in {"stopped", "stop", "offline"}:
                if self.encoder or self.audio_feeder or self.visual_feeder:
                    self.cleanup()
                self.current_generation = generation
                self.write_health("stopped", desired)
                time.sleep(2)
                continue

            # generation is reserved for explicit start/restart operations.
            # Visual-only changes use visual_revision and MUST NOT restart RTMP.
            if self.current_generation is not None and generation != self.current_generation:
                self.restarts += 1
                self.cleanup()

            try:
                if not self.audio_feeder or self.audio_feeder.poll() is not None:
                    self.start_audio()
                if not self.visual_feeder or self.visual_feeder.poll() is not None:
                    self.start_visual()
                if not self.encoder or self.encoder.poll() is not None:
                    if self.current_generation is not None:
                        self.restarts += 1
                    self.start_encoder()
                self.current_generation = generation
            except Exception as exc:
                self.write_health("restarting", desired, {"error": str(exc)[:500]})
                time.sleep(min(20, 2 + self.restarts))
                continue

            status = "live" if self.connected_since and time.time() - self.connected_since >= 30 else "starting"
            self.write_health(status, desired)
            time.sleep(2)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--platform",
        required=True,
        choices=["kick", "twitch", "youtube-deep-house", "youtube-rainy", "youtube-ui-test"],
    )
    args = ap.parse_args()
    core = StreamCore(args.platform)

    def stop(*_):
        core.running = False
        core.cleanup()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    core.validate()
    try:
        core.monitor()
    finally:
        core.cleanup()


if __name__ == "__main__":
    main()
