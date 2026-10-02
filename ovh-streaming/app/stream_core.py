#!/usr/bin/env python3
import argparse
import json
import os
import pathlib
import signal
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timezone


def iso_now():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def atomic_json(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


class StreamCore:
    def __init__(self, platform):
        self.platform = platform
        self.state = pathlib.Path("/state") / platform
        self.state.mkdir(parents=True, exist_ok=True)
        self.health_path = self.state / "health.json"
        self.stream_url = os.environ.get("STREAM_URL", "").rstrip("/")
        self.stream_key = os.environ.get("STREAM_KEY", "").strip()
        self.loop_url = os.environ.get("LOOP_URL", "").strip()
        self.playlist_file = os.environ.get("PLAYLIST_FILE", "/config/gaming.json")
        self.fps = int(os.environ.get("VIDEO_FPS", "60" if platform == "kick" else "30"))
        self.vbitrate = int(os.environ.get("VIDEO_BITRATE_KBPS", "8000" if platform == "kick" else "4500"))
        self.bufsize = int(os.environ.get("VIDEO_BUFSIZE_KBPS", str(self.vbitrate * 2)))
        self.abitrate = int(os.environ.get("AUDIO_BITRATE_KBPS", "160"))
        self.encoder = None
        self.feeder = None
        self.fd = None
        self.running = True
        self.restarts = 0

    def validate(self):
        if not self.stream_url:
            raise RuntimeError("STREAM_URL is missing")
        if not self.stream_key:
            raise RuntimeError("STREAM_KEY is missing")
        if not self.loop_url:
            raise RuntimeError("LOOP_URL is missing")
        if not pathlib.Path(self.playlist_file).exists():
            raise RuntimeError(f"playlist file not found: {self.playlist_file}")

    def download_visual(self):
        source = self.state / "visual-source"
        loop = self.state / "loop.mp4"
        req = urllib.request.Request(self.loop_url, headers={"User-Agent": f"PeterLofi-OVH-{self.platform}"})
        with urllib.request.urlopen(req, timeout=180) as r, open(source, "wb") as f:
            while True:
                chunk = r.read(1024 * 1024)
                if not chunk:
                    break
                f.write(chunk)

        probe = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=codec_type",
             "-of", "default=nw=1:nk=1", str(source)],
            capture_output=True, text=True
        )
        if probe.returncode == 0 and "video" in probe.stdout:
            loop.write_bytes(source.read_bytes())
        else:
            subprocess.run([
                "ffmpeg", "-hide_banner", "-loglevel", "warning", "-y",
                "-loop", "1", "-i", str(source), "-t", "12",
                "-vf", f"scale=1920:1080:force_original_aspect_ratio=decrease:flags=lanczos,"
                       f"pad=1920:1080:(ow-iw)/2:(oh-ih)/2,setsar=1,fps={self.fps},format=yuv420p",
                "-an", "-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
                "-g", str(self.fps * 2), "-keyint_min", str(self.fps * 2), "-sc_threshold", "0",
                str(loop)
            ], check=True)
        return loop

    def target(self):
        return f"{self.stream_url}/{self.stream_key.lstrip('/')}"

    def cleanup(self):
        for proc in (self.encoder, self.feeder):
            if proc and proc.poll() is None:
                try:
                    proc.send_signal(signal.SIGINT)
                except Exception:
                    pass
        time.sleep(0.5)
        for proc in (self.encoder, self.feeder):
            if proc and proc.poll() is None:
                proc.kill()
        if self.fd is not None:
            try:
                os.close(self.fd)
            except Exception:
                pass
        self.encoder = self.feeder = None
        self.fd = None

    def start(self, loop):
        self.cleanup()
        fifo = self.state / "audio.pcm"
        try:
            fifo.unlink()
        except FileNotFoundError:
            pass
        os.mkfifo(fifo)
        self.fd = os.open(fifo, os.O_RDWR)

        self.feeder = subprocess.Popen([
            sys.executable, "/app/audio_engine.py",
            "--platform", self.platform,
            "--playlist-file", self.playlist_file,
            "--state-dir", "/state",
        ], stdout=self.fd, stderr=subprocess.DEVNULL)

        gop = self.fps * 2
        cmd = [
            "ffmpeg", "-hide_banner", "-loglevel", "warning",
            "-re", "-stream_loop", "-1", "-i", str(loop),
            "-thread_queue_size", "1024", "-f", "s16le", "-ar", "48000", "-ac", "2", "-i", str(fifo),
            "-map", "0:v:0", "-map", "1:a:0",
            "-vf", f"scale=1920:1080:force_original_aspect_ratio=decrease:flags=lanczos,"
                   f"pad=1920:1080:(ow-iw)/2:(oh-ih)/2,setsar=1,fps={self.fps},format=yuv420p",
            "-r", str(self.fps), "-s:v", "1920x1080", "-pix_fmt", "yuv420p",
            "-c:v", "libx264", "-preset", "veryfast", "-tune", "zerolatency",
            "-profile:v", "high", "-b:v", f"{self.vbitrate}k",
            "-minrate", f"{self.vbitrate}k", "-maxrate", f"{self.vbitrate}k",
            "-bufsize", f"{self.bufsize}k", "-g", str(gop), "-keyint_min", str(gop),
            "-sc_threshold", "0", "-x264-params", "nal-hrd=cbr:force-cfr=1",
            "-c:a", "aac", "-b:a", f"{self.abitrate}k", "-ar", "48000", "-ac", "2",
            "-flvflags", "no_duration_filesize", "-f", "flv", self.target()
        ]
        self.encoder = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
        atomic_json(self.health_path, {
            "platform": self.platform,
            "status": "starting",
            "encoder_pid": self.encoder.pid,
            "audio_pid": self.feeder.pid,
            "fps": self.fps,
            "video_bitrate_kbps": self.vbitrate,
            "started_at": iso_now(),
            "restarts": self.restarts,
        })

    def monitor(self):
        loop = self.download_visual()
        self.start(loop)
        connected_since = time.time()
        while self.running:
            enc_ok = self.encoder and self.encoder.poll() is None
            feed_ok = self.feeder and self.feeder.poll() is None
            if not enc_ok or not feed_ok:
                self.restarts += 1
                atomic_json(self.health_path, {
                    "platform": self.platform,
                    "status": "restarting",
                    "encoder_alive": bool(enc_ok),
                    "audio_alive": bool(feed_ok),
                    "restarts": self.restarts,
                    "updated_at": iso_now(),
                })
                time.sleep(min(15, 2 + self.restarts))
                self.start(loop)
                connected_since = time.time()
                continue

            status = "live" if time.time() - connected_since >= 30 else "starting"
            atomic_json(self.health_path, {
                "platform": self.platform,
                "status": status,
                "encoder_pid": self.encoder.pid,
                "audio_pid": self.feeder.pid,
                "fps": self.fps,
                "video_bitrate_kbps": self.vbitrate,
                "restarts": self.restarts,
                "updated_at": iso_now(),
            })
            time.sleep(5)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--platform", required=True, choices=["kick", "twitch"])
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
