#!/usr/bin/env python3
import argparse
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
        for item in data.get("tracks") or []:
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
        try:
            while self.running:
                action = self.read_command()
                if action:
                    interrupted = action
                    proc.terminate()
                    break
                ready, _, _ = select.select([src], [], [], 0.25)
                if not ready:
                    if proc.poll() is not None:
                        break
                    continue
                chunk = os.read(src, PCM_CHUNK)
                if not chunk:
                    break
                view = memoryview(chunk)
                while view:
                    written = os.write(dst, view)
                    view = view[written:]
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
    ap.add_argument("--platform", required=True, choices=["kick", "twitch"])
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
