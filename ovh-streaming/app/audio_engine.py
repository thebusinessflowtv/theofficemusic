#!/usr/bin/env python3
import argparse
import array
import base64
import hashlib
import json
import os
import pathlib
import queue
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
AUDIO_CROSSFADE_SECONDS = max(0.25, float(os.environ.get("AUDIO_CROSSFADE_SECONDS", "1.5")))
AUDIO_FRAME_SECONDS = 0.020
AUDIO_FRAME_FRAMES = int(PCM_RATE * AUDIO_FRAME_SECONDS)
AUDIO_FRAME_BYTES = AUDIO_FRAME_FRAMES * PCM_CHANNELS * 2
AUDIO_PREBUFFER_SECONDS = max(0.25, float(os.environ.get("AUDIO_PREBUFFER_SECONDS", "0.75")))
AUDIO_PREBUFFER_FRAMES = max(4, int(AUDIO_PREBUFFER_SECONDS / AUDIO_FRAME_SECONDS))
READ_TIMEOUT = object()


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


def read_json(path, default=None):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


class Playlist:
    def __init__(self, path):
        self.path = pathlib.Path(path)
        self.mtime = None
        self.tracks = []
        self.shuffle = True
        self.playlist_key = ""
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
                "source": str(item.get("source") or ""),
                "artists": str(item.get("artists") or ""),
                "duration_seconds": float(item.get("duration_seconds") or 0),
            })
        if not tracks:
            raise RuntimeError("playlist has no playable tracks")
        self.tracks = tracks
        self.shuffle = bool(data.get("shuffle", True))
        self.playlist_key = str(data.get("playlist_key") or "")
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
            if self.playlist_key == "twitch-dj-mixed":
                commercial = [t["id"] for t in self.tracks if t.get("source") == "twitch_dj_catalog_licensed_copy" or (t["id"].startswith("twitch-dj-") and not t["id"].startswith("twitch-dj-original-"))]
                original = [t["id"] for t in self.tracks if t["id"] not in set(commercial)]
                random.shuffle(commercial)
                random.shuffle(original)

                # Balanced Twitch DJ shuffle: random order inside each group,
                # but no long runs of only original tracks. With 36 originals
                # and 35 catalog tracks this produces an almost perfect 1:1 mix.
                self.bag = []
                pools = [original, commercial] if random.choice((True, False)) else [commercial, original]
                while pools[0] or pools[1]:
                    for pool in pools:
                        if pool:
                            self.bag.append(pool.pop())
                if len(self.bag) > 1 and self.bag[0] == self.last_id:
                    self.bag.append(self.bag.pop(0))
            else:
                self.bag = [t["id"] for t in self.tracks]
                random.shuffle(self.bag)
                if len(self.bag) > 1 and self.bag[0] == self.last_id:
                    self.bag.append(self.bag.pop(0))
        track = self.by_id(self.bag.pop(0)) or self.tracks[0]
        self.last_id = track["id"]
        return track



class PCMDecoder:
    """Decode ahead into exact 20 ms PCM frames.

    The persistent encoder is the only clock. Decoders may run ahead, but the
    AudioEngine emits exactly one fixed-size PCM frame per clock tick.
    """

    def __init__(self, engine, track):
        self.engine = engine
        self.track = track
        self.cached = False
        self.proc = None
        self.q = queue.Queue(maxsize=400)
        self.stop_event = threading.Event()
        self.eof = False
        source, self.cached = engine.source_for(track)
        cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin"]
        if not self.cached:
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
        self.proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=0)
        self.thread = threading.Thread(
            target=self._reader,
            name=f"pcm-{engine.platform}-{track['id']}",
            daemon=True,
        )
        self.thread.start()

    def _put(self, item):
        while not self.stop_event.is_set():
            try:
                self.q.put(item, timeout=0.1)
                return True
            except queue.Full:
                continue
        return False

    def _reader(self):
        buf = bytearray()
        try:
            while not self.stop_event.is_set():
                chunk = self.proc.stdout.read(65536)
                if not chunk:
                    break
                buf.extend(chunk)
                while len(buf) >= AUDIO_FRAME_BYTES and not self.stop_event.is_set():
                    frame = bytes(buf[:AUDIO_FRAME_BYTES])
                    del buf[:AUDIO_FRAME_BYTES]
                    if not self._put(frame):
                        return
            if buf and not self.stop_event.is_set():
                frame = bytes(buf[:AUDIO_FRAME_BYTES]).ljust(AUDIO_FRAME_BYTES, b"\x00")
                self._put(frame)
        finally:
            if not self.stop_event.is_set():
                self._put(None)

    def buffered_frames(self):
        return self.q.qsize()

    def read_frame(self):
        if self.eof:
            return None
        try:
            item = self.q.get_nowait()
        except queue.Empty:
            return READ_TIMEOUT
        if item is None:
            self.eof = True
            return None
        return item

    def stop(self):
        self.stop_event.set()
        if self.proc and self.proc.poll() is None:
            try:
                self.proc.terminate()
                self.proc.wait(timeout=0.35)
            except Exception:
                try:
                    self.proc.kill()
                except Exception:
                    pass
        try:
            if self.proc and self.proc.stdout:
                self.proc.stdout.close()
        except Exception:
            pass


def mix_crossfade_frame(current_frame, next_frame, fade_index, fade_total):
    a = array.array("h")
    b = array.array("h")
    a.frombytes((current_frame or b"").ljust(AUDIO_FRAME_BYTES, b"\x00")[:AUDIO_FRAME_BYTES])
    b.frombytes((next_frame or b"").ljust(AUDIO_FRAME_BYTES, b"\x00")[:AUDIO_FRAME_BYTES])
    if sys.byteorder != "little":
        a.byteswap()
        b.byteswap()
    p = max(0.0, min(1.0, fade_index / max(1, fade_total - 1)))
    ga = 1.0 - p
    gb = p
    for idx in range(len(a)):
        value = int(a[idx] * ga + b[idx] * gb)
        a[idx] = max(-32768, min(32767, value))
    if sys.byteorder != "little":
        a.byteswap()
    return a.tobytes()

class AudioEngine:
    def __init__(self, platform, playlist_file, state_dir):
        self.platform = platform
        self.playlist = Playlist(playlist_file)
        self.state = pathlib.Path(state_dir) / platform
        self.now_path = self.state / "now-playing.json"
        self.command_path = self.state / "command.json"
        existing_command = read_json(self.command_path, {}) or {}
        self.last_command_id = str(existing_command.get("id") or "") or None
        self.history = []
        self.forced_next = None
        self.running = True
        # Restore recent track history after an audio-feeder restart so the
        # MediaForge "Anterior" control keeps working across hot patches.
        try:
            saved = read_json(self.now_path, {}) or {}
            for track_id in saved.get("history") or []:
                track = self.playlist.by_id(str(track_id))
                if track:
                    self.history.append(track)
            current = self.playlist.by_id(str(saved.get("track_id") or ""))
            if current and (not self.history or self.history[-1]["id"] != current["id"]):
                self.history.append(current)
            self.history = self.history[-50:]
        except Exception:
            self.history = []
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
            if not local.exists():
                marker = "/ovh-streaming/state/"
                raw = str(local)
                if marker in raw:
                    suffix = raw.split(marker, 1)[1]
                    candidate = pathlib.Path("/state") / suffix
                    if candidate.exists():
                        local = candidate
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
        # Do not discard PCM when the encoder applies brief backpressure.
        # Dropping even a small PCM block is audible as a click/stutter.
        # Wait for writability and preserve every sample instead.
        view = memoryview(chunk)
        wait_started = time.monotonic()
        while view and self.running:
            try:
                _, writable, _ = select.select([], [dst], [], 0.25)
                if not writable:
                    self.write_audio_health({
                        "state": "encoder_backpressure_buffering",
                        "backpressure_wait_seconds": round(time.monotonic() - wait_started, 3),
                    })
                    continue
                written = os.write(dst, view)
                if written > 0:
                    view = view[written:]
            except BlockingIOError:
                time.sleep(0.005)
            except InterruptedError:
                continue
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
            "artists": track.get("artists") or "",
            "source": track.get("source") or "",
            "url": track["url"],
            "started_at": iso_now(),
            "history": [x["id"] for x in self.history[-10:]],
        })

    def silence_chunk(self, seconds=AUDIO_FRAME_SECONDS):
        if abs(seconds - AUDIO_FRAME_SECONDS) < 0.0001:
            return bytes(AUDIO_FRAME_BYTES)
        frames = max(1, int(seconds * PCM_RATE))
        return bytes(frames * PCM_CHANNELS * 2)

    def target_for_action(self, action):
        if action == "previous":
            prev = self.previous_track()
            if prev:
                self.playlist.last_id = prev["id"]
                return prev
        return self.playlist.next()

    def start_transition(self, current_track, action):
        attempts = min(12, max(1, len(self.playlist.tracks)))
        last_error = None
        for _ in range(attempts):
            target = self.target_for_action(action)
            try:
                upcoming = PCMDecoder(self, target)
                self.write_audio_health({
                    "state": "prebuffering_transition",
                    "track_id": current_track["id"],
                    "next_track_id": target["id"],
                    "reason": action,
                })
                return {
                    "decoder": upcoming,
                    "track": target,
                    "reason": action,
                    "started": False,
                    "fade_index": 0,
                }
            except Exception as exc:
                last_error = str(exc)
                self.write_audio_health({
                    "state": "skipping_unplayable_track",
                    "track_id": current_track["id"],
                    "bad_track_id": target["id"],
                    "reason": action,
                    "error": last_error[:500],
                })
                action = "skip"
        self.write_audio_health({
            "state": "transition_failed_keep_current",
            "track_id": current_track["id"],
            "reason": action,
            "error": (last_error or "no playable next track")[:500],
        })
        return None

    def run(self):
        dst = sys.stdout.fileno()
        silence = bytes(AUDIO_FRAME_BYTES)
        fade_total = max(1, int(AUDIO_CROSSFADE_SECONDS / AUDIO_FRAME_SECONDS))

        decoder = None
        track = None
        for _ in range(min(20, max(1, len(self.playlist.tracks)))):
            candidate = self.choose()
            try:
                decoder = PCMDecoder(self, candidate)
                track = candidate
                break
            except Exception as exc:
                self.write_audio_health({
                    "state": "startup_skipping_unplayable_track",
                    "bad_track_id": candidate["id"],
                    "error": str(exc)[:500],
                })
        if decoder is None or track is None:
            raise RuntimeError("no playable audio tracks available")
        self.history.append(track)
        self.history = self.history[-50:]
        self.publish(track)
        pending = None
        last_audio_at = time.monotonic()
        next_tick = time.monotonic()

        self.write_audio_health({
            "state": "playing",
            "track_id": track["id"],
            "cached": decoder.cached,
            "clock_frame_ms": int(AUDIO_FRAME_SECONDS * 1000),
        })

        try:
            while self.running:
                # Fixed wall-clock audio cadence: one 20 ms PCM frame every
                # 20 ms, regardless of decoder startup/track changes.
                now = time.monotonic()
                if now < next_tick:
                    time.sleep(next_tick - now)
                elif now - next_tick > 0.5:
                    next_tick = now
                next_tick += AUDIO_FRAME_SECONDS

                action = self.read_command()
                if action and pending is None:
                    pending = self.start_transition(track, action)

                current_frame = decoder.read_frame()
                current_eof = current_frame is None
                if current_frame is READ_TIMEOUT or current_eof:
                    current_frame = silence
                else:
                    last_audio_at = time.monotonic()

                if current_eof and pending is None:
                    pending = self.start_transition(track, "natural_end")

                if pending is not None:
                    upcoming = pending["decoder"]

                    # Never start the fade until the next decoder has a healthy
                    # buffer. Current audio continues while it fills.
                    if not pending["started"]:
                        if upcoming.buffered_frames() >= AUDIO_PREBUFFER_FRAMES:
                            pending["started"] = True
                            pending["fade_index"] = 0
                            self.history.append(pending["track"])
                            self.history = self.history[-50:]
                            self.publish(pending["track"], state="crossfading")
                        elif upcoming.eof:
                            upcoming.stop()
                            self.write_audio_health({
                                "state": "transition_prebuffer_failed",
                                "track_id": track["id"],
                                "next_track_id": pending["track"]["id"],
                            })
                            pending = None

                    if pending is not None and pending["started"]:
                        next_frame = upcoming.read_frame()
                        if next_frame is READ_TIMEOUT or next_frame is None:
                            # The prebuffer target makes this exceptional, but
                            # silence still preserves the continuous PCM clock.
                            next_frame = silence
                        out = mix_crossfade_frame(
                            current_frame,
                            next_frame,
                            pending["fade_index"],
                            fade_total,
                        )
                        pending["fade_index"] += 1
                        if pending["fade_index"] >= fade_total:
                            old = decoder
                            decoder = upcoming
                            track = pending["track"]
                            pending = None
                            old.stop()
                            self.publish(track, state="playing")
                            self.write_audio_health({
                                "state": "playing",
                                "track_id": track["id"],
                                "transition": "crossfade",
                                "crossfade_seconds": AUDIO_CROSSFADE_SECONDS,
                                "prebuffer_seconds": AUDIO_PREBUFFER_SECONDS,
                                "clock_frame_ms": int(AUDIO_FRAME_SECONDS * 1000),
                                "cached": decoder.cached,
                            })
                    else:
                        out = current_frame
                else:
                    out = current_frame

                self.write_pcm(dst, out)

                if time.monotonic() - last_audio_at >= AUDIO_STALL_TIMEOUT_SECONDS:
                    self.stalls += 1
                    self.write_audio_health({
                        "state": "source_stalled_pcm_clock_preserved",
                        "track_id": track["id"],
                        "stall_seconds": round(time.monotonic() - last_audio_at, 2),
                    })
                    if pending is None:
                        pending = self.start_transition(track, "source_stall")
                    last_audio_at = time.monotonic()

        except BrokenPipeError:
            self.running = False
        finally:
            decoder.stop()
            if pending is not None:
                pending["decoder"].stop()
            self.write_audio_health({"state": "stopped", "track_id": track["id"]})


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
