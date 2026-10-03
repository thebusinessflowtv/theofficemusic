#!/usr/bin/env python3
import argparse
import array
import base64
import json
import os
import pathlib
import random
import select
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone

PCM_CHUNK = 32768
PCM_RATE = 48000
PCM_CHANNELS = 2
FADE_IN_SECONDS = max(0.0, float(os.environ.get("AUDIO_FADE_IN_SECONDS", "1.5")))
FADE_OUT_SECONDS = max(0.0, float(os.environ.get("AUDIO_FADE_OUT_SECONDS", "1.5")))


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
        cmd = [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin",
            "-reconnect", "1", "-reconnect_streamed", "1", "-reconnect_delay_max", "5",
            "-i", track["url"],
            "-vn", "-sn", "-dn",
            "-af", "aresample=48000:async=1:first_pts=0",
            "-ac", "2", "-ar", "48000", "-c:a", "pcm_s16le", "-f", "s16le", "pipe:1",
        ]
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=0)
        src = proc.stdout.fileno()
        dst = sys.stdout.fileno()
        interrupted = None

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

                ready, _, _ = select.select([src], [], [], 0.25)
                if not ready:
                    if proc.poll() is not None:
                        break
                    continue

                chunk = os.read(src, PCM_CHUNK)
                if not chunk:
                    break

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

                view = memoryview(chunk)
                while view:
                    written = os.write(dst, view)
                    view = view[written:]

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
