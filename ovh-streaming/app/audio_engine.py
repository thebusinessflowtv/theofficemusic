#!/usr/bin/env python3
import argparse
import array
import base64
import hashlib
import json
import os
import pathlib
import random
import select
import signal
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone

PCM_CHUNK = 32768
PCM_RATE = 48000
PCM_CHANNELS = 2
FADE_IN_SECONDS = max(0.0, float(os.environ.get("AUDIO_FADE_IN_SECONDS", "1.5")))
FADE_OUT_SECONDS = max(0.0, float(os.environ.get("AUDIO_FADE_OUT_SECONDS", "1.5")))
AUDIO_STALL_TIMEOUT_SECONDS = max(3.0, float(os.environ.get("AUDIO_STALL_TIMEOUT_SECONDS", "8")))
AUDIO_HTTP_RW_TIMEOUT_SECONDS = max(5.0, float(os.environ.get("AUDIO_HTTP_RW_TIMEOUT_SECONDS", "15")))
AUDIO_SILENCE_INTERVAL_SECONDS = 0.25
AUDIO_CACHE_MIN_BYTES = 4096


def pcm_frames(chunk):
    return len(chunk) // (2 * PCM_CHANNELS)


def apply_gain_ramp(chunk, start_gain, end_gain):
    """Apply a linear gain ramp to signed 16-bit little-endian stereo PCM."""
    if not chunk or (start_gain >= 0.9999 and end_gain >= 0.9999):
        return chunk
    samples = array.array("h")
    samples.frombytes(chunk)
    if sys.byteorder != "little":
        samples.byteswap()
    frames = max(1, len(samples) // PCM_CHANNELS)
    denom = max(1, frames - 1)
    sg = max(0.0, min(1.0, float(start_gain)))
    eg = max(0.0, min(1.0, float(end_gain)))
    step = (eg - sg) / denom
    for frame in range(frames):
        gain = sg + step * frame
        base = frame * PCM_CHANNELS
        for ch in range(PCM_CHANNELS):
            idx = base + ch
            value = int(samples[idx] * gain)
            samples[idx] = max(-32768, min(32767, value))
    if sys.byteorder != "little":
        samples.byteswap()
    return samples.tobytes()


def iso_now():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def atomic_json(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


class Playlist:
    def __init__(self, path):
        self.path = pathlib.Path(path)
        self.mtime = None
        self.tracks = []
        self.shuffle = True
        self.bag = []
        self.last_id = None
        self.reload(force=True)

    def reload(self, force=False):
        st = self.path.stat()
        if not force and self.mtime == st.st_mtime_ns:
            return
        data = json.loads(self.path.read_text(encoding="utf-8"))
        tracks = []
        raw_tracks = data.get("tracks") or []
        if not raw_tracks and data.get("track_urls_b64"):
            urls = json.loads(base64.b64decode(data["track_urls_b64"]).decode("utf-8"))
            raw_tracks = [
                {"id": f"track-{i+1:02d}", "title": f"Track {i+1:02d}", "url": url}
                for i, url in enumerate(urls)
            ]
        for item in raw_tracks:
            if not isinstance(item, dict) or not item.get("url"):
                continue
            tracks.append({
                "id": str(item.get("id") or item["url"]),
                "title": str(item.get("title") or item.get("id") or "Track"),
                "url": str(item["url"]),
            })
        if not tracks:
            raise RuntimeError("playlist has no playable tracks")
        self.tracks = tracks
        self.shuffle = bool(data.get("shuffle", True))
        self.mtime = st.st_mtime_ns
        self.bag = []

    def by_id(self, track_id):
        for t in self.tracks:
            if t["id"] == track_id:
                return t
        return None

    def next(self):
        self.reload()
        if not self.shuffle:
            if self.last_id is None:
                track = self.tracks[0]
            else:
                idx = next((i for i,t in enumerate(self.tracks) if t["id"] == self.last_id), -1)
                track = self.tracks[(idx + 1) % len(self.tracks)]
            self.last_id = track["id"]
            return track

        if not self.bag:
            self.bag = [t["id"] for t in self.tracks]
            random.shuffle(self.bag)
            if len(self.bag) > 1 and self.bag[0] == self.last_id:
                self.bag.append(self.bag.pop(0))
        track = self.by_id(self.bag.pop(0)) or self.tracks[0]
        self.last_id = track["id"]
        return track


class AudioEngine:
    def __init__(self, platform, playlist_file, state_dir):
        self.platform = platform
        self.playlist = Playlist(playlist_file)
        self.state = pathlib.Path(state_dir) / platform
        self.now_path = self.state / "now-playing.json"
        self.command_path = self.state / "command.json"
        self.last_command_id = None
        self.history = []
        self.forced_next = None
        self.running = True
        self.cache_dir = pathlib.Path(state_dir) / "audio-cache"
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.audio_health_path = self.state / "audio-health.json"
        self.cache_hits = 0
        self.cache_misses = 0
        self.stalls = 0
        self.backpressure_drops = 0
        self.cache_thread = threading.Thread(target=self.warm_cache, name=f"audio-cache-{platform}", daemon=True)
        self.cache_thread.start()

    def write_audio_health(self, extra=None):
        payload = {
            "platform": self.platform,
            "status": "running" if self.running else "stopped",
            "cache_hits": self.cache_hits,
            "cache_misses": self.cache_misses,
            "stalls": self.stalls,
            "backpressure_drops": self.backpressure_drops,
            "updated_at": iso_now(),
        }
        if extra:
            payload.update(extra)
        atomic_json(self.audio_health_path, payload)

    def cache_path(self, track):
        parsed = urllib.parse.urlparse(track["url"])
        suffix = pathlib.Path(parsed.path).suffix.lower()
        if not suffix or len(suffix) > 10:
            suffix = ".media"
        token = hashlib.sha256(track["url"].encode("utf-8")).hexdigest()[:32]
        return self.cache_dir / f"{token}{suffix}"

    def cache_ready(self, path):
        try:
            return path.exists() and path.stat().st_size >= AUDIO_CACHE_MIN_BYTES
        except Exception:
            return False

    def download_to_cache(self, track):
        target = self.cache_path(track)
        if self.cache_ready(target):
            return target
        lock = target.with_suffix(target.suffix + ".lock")
        try:
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.close(fd)
        except FileExistsError:
            try:
                if time.time() - lock.stat().st_mtime > 300:
                    lock.unlink(missing_ok=True)
            except Exception:
                pass
            return target if self.cache_ready(target) else None

        temp = target.with_suffix(target.suffix + f".{self.platform}.part")
        temp.unlink(missing_ok=True)
        try:
            req = urllib.request.Request(track["url"], headers={"User-Agent": f"MediaForge-AudioCache-{self.platform}"})
            with urllib.request.urlopen(req, timeout=AUDIO_HTTP_RW_TIMEOUT_SECONDS) as response, open(temp, "wb") as fh:
                while self.running:
                    chunk = response.read(1024 * 1024)
                    if not chunk:
                        break
                    fh.write(chunk)
            if not self.running:
                return None
            if not temp.exists() or temp.stat().st_size < AUDIO_CACHE_MIN_BYTES:
                raise RuntimeError("downloaded audio is empty")
            probe = subprocess.run(
                ["ffprobe", "-v", "error", "-select_streams", "a:0", "-show_entries", "stream=codec_type", "-of", "default=nw=1:nk=1", str(temp)],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, timeout=20,
            )
            if probe.returncode != 0 or "audio" not in probe.stdout:
                raise RuntimeError("downloaded file has no audio stream")
            if self.cache_ready(target):
                temp.unlink(missing_ok=True)
            else:
                temp.replace(target)
            return target
        except Exception:
            temp.unlink(missing_ok=True)
            return None
        finally:
            lock.unlink(missing_ok=True)

    def warm_cache(self):
        while self.running:
            try:
                self.playlist.reload()
                for track in list(self.playlist.tracks):
                    if not self.running:
                        break
                    parsed = urllib.parse.urlparse(track["url"])
                    if parsed.scheme == "file":
                        continue
                    if self.cache_ready(self.cache_path(track)):
                        continue
                    self.download_to_cache(track)
            except Exception:
                pass
            self.write_audio_health({"cache_warm": True})
            for _ in range(60):
                if not self.running:
                    return
                time.sleep(1)

    def source_for(self, track):
        parsed = urllib.parse.urlparse(track["url"])
        if parsed.scheme == "file":
            local = pathlib.Path(urllib.parse.unquote(parsed.path))
            if not local.exists() or local.stat().st_size < AUDIO_CACHE_MIN_BYTES:
                raise RuntimeError(f"local DJ audio missing: {local}")
            self.cache_hits += 1
            return str(local), True
        cached = self.cache_path(track)
        if self.cache_ready(cached):
            self.cache_hits += 1
            return str(cached), True
        self.cache_misses += 1
        return track["url"], False

    def write_pcm(self, dst, chunk, deadline_seconds=1.0):
        view = memoryview(chunk)
        deadline = time.monotonic() + deadline_seconds
        while view and self.running:
            try:
                written = os.write(dst, view)
                view = view[written:]
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    self.backpressure_drops += 1
                    self.write_audio_health({"state": "encoder_backpressure"})
                    return False
                time.sleep(0.01)
        return not view

    def read_command(self):
        try:
            data = json.loads(self.command_path.read_text(encoding="utf-8"))
        except Exception:
            return None
        cid = str(data.get("id") or "")
        if not cid or cid == self.last_command_id:
            return None
        self.last_command_id = cid
        action = str(data.get("action") or "").lower()
        if action not in {"skip", "previous"}:
            return None
        return action

    def choose(self):
        if self.forced_next:
            track = self.playlist.by_id(self.forced_next)
            self.forced_next = None
            if track:
                self.playlist.last_id = track["id"]
                return track
        return self.playlist.next()

    def previous_track(self):
        if len(self.history) < 2:
            return None
        return self.history[-2]

    def publish(self, track, state="playing"):
        atomic_json(self.now_path, {
            "platform": self.platform,
            "state": state,
            "track_id": track["id"],
            "title": track["title"],
            "url": track["url"],
            "started_at": iso_now(),
            "history": [x["id"] for x in self.history[-10:]],
        })

    def decode(self, track):
        source, cached = self.source_for(track)
        # Pace decode at wall-clock speed. Without -re, local/cached MP3s are
        # decoded much faster than the persistent encoder can consume PCM,
        # filling the FIFO and forcing write_pcm() to drop chunks. Those drops
        # are heard as short stutters on every platform.
        cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-re"]
        if not cached:
            cmd += [
                "-rw_timeout", str(int(AUDIO_HTTP_RW_TIMEOUT_SECONDS * 1_000_000)),
                "-reconnect", "1",
                "-reconnect_streamed", "1",
                "-reconnect_on_network_error", "1",
                "-reconnect_on_http_error", "4xx,5xx",
                "-reconnect_delay_max", "5",
            ]
        cmd += [
            "-i", source,
            "-vn", "-sn", "-dn",
            "-af", "aresample=48000:async=1:first_pts=0",
            "-ac", "2", "-ar", "48000", "-c:a", "pcm_s16le", "-f", "s16le", "pipe:1",
        ]
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=0)
        src = proc.stdout.fileno()
        dst = sys.stdout.fileno()
        try:
            os.set_blocking(dst, False)
        except Exception:
            pass
        interrupted = None
        last_audio_at = time.monotonic()
        silence_frames = max(1, int(AUDIO_SILENCE_INTERVAL_SECONDS * PCM_RATE))
        silence_chunk = bytes(silence_frames * PCM_CHANNELS * 2)
        self.write_audio_health({"state": "playing", "track_id": track["id"], "cached": cached})

        fade_in_total = int(FADE_IN_SECONDS * PCM_RATE)
        fade_out_total = int(FADE_OUT_SECONDS * PCM_RATE)
        played_frames = 0
        fading_out = False
        fade_out_done = 0
        fade_out_start_gain = 1.0

        try:
            while self.running:
                if not fading_out:
                    action = self.read_command()
                    if action:
                        interrupted = action
                        if fade_out_total <= 0:
                            proc.terminate()
                            break
                        fading_out = True
                        if fade_in_total > 0 and played_frames < fade_in_total:
                            fade_out_start_gain = max(0.0, min(1.0, played_frames / fade_in_total))
                        else:
                            fade_out_start_gain = 1.0

                ready, _, _ = select.select([src], [], [], AUDIO_SILENCE_INTERVAL_SECONDS)
                if not ready:
                    if proc.poll() is not None:
                        break
                    # Never let a slow CDN/HTTP connection starve the persistent encoder.
                    self.write_pcm(dst, silence_chunk, deadline_seconds=AUDIO_SILENCE_INTERVAL_SECONDS)
                    if time.monotonic() - last_audio_at >= AUDIO_STALL_TIMEOUT_SECONDS:
                        self.stalls += 1
                        self.write_audio_health({
                            "state": "source_stalled",
                            "track_id": track["id"],
                            "stall_seconds": round(time.monotonic() - last_audio_at, 2),
                            "cached": cached,
                        })
                        try:
                            proc.terminate()
                        except Exception:
                            pass
                        break
                    continue

                chunk = os.read(src, PCM_CHUNK)
                if not chunk:
                    break
                last_audio_at = time.monotonic()

                frames = pcm_frames(chunk)
                if frames <= 0:
                    continue

                if fading_out:
                    start = fade_out_start_gain * max(0.0, 1.0 - (fade_out_done / max(1, fade_out_total)))
                    end = fade_out_start_gain * max(0.0, 1.0 - ((fade_out_done + frames) / max(1, fade_out_total)))
                    chunk = apply_gain_ramp(chunk, start, end)
                    fade_out_done += frames
                elif fade_in_total > 0 and played_frames < fade_in_total:
                    start = max(0.0, min(1.0, played_frames / fade_in_total))
                    end = max(0.0, min(1.0, (played_frames + frames) / fade_in_total))
                    chunk = apply_gain_ramp(chunk, start, end)

                self.write_pcm(dst, chunk)
                played_frames += frames

                if fading_out and fade_out_done >= fade_out_total:
                    proc.terminate()
                    break

        except BrokenPipeError:
            self.running = False
        finally:
            try:
                proc.wait(timeout=4)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
            self.write_audio_health({"state": "track_finished", "track_id": track["id"], "cached": cached})
        return interrupted

    def run(self):
        while self.running:
            track = self.choose()
            self.history.append(track)
            self.history = self.history[-50:]
            self.publish(track)
            action = self.decode(track)
            if action == "previous":
                prev = self.previous_track()
                if prev:
                    self.forced_next = prev["id"]
            elif action == "skip":
                pass
            time.sleep(0.05)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--platform", required=True, choices=["kick", "twitch", "youtube-deep-house", "youtube-rainy"])
    ap.add_argument("--playlist-file", required=True)
    ap.add_argument("--state-dir", default="/state")
    args = ap.parse_args()
    engine = AudioEngine(args.platform, args.playlist_file, args.state_dir)

    def stop(*_):
        engine.running = False
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    engine.run()


if __name__ == "__main__":
    main()
