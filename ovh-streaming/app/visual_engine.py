#!/usr/bin/env python3
import argparse
import hashlib
import json
import os
import pathlib
import signal
import subprocess
import time
import urllib.request
import urllib.parse
import shutil
import textwrap
from datetime import datetime, timezone


def iso_now():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def read_json(path, default=None):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def atomic_json(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


class VisualEngine:
    def __init__(self, platform, state_dir):
        self.platform = platform
        self.state = pathlib.Path(state_dir) / platform
        self.state.mkdir(parents=True, exist_ok=True)
        self.desired_path = self.state / "desired.json"
        self.health_path = self.state / "visual-health.json"
        self.cache_dir = self.state / "visual-cache"
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.base_loop_url = os.environ.get("LOOP_URL", "").strip()
        self.fps = int(os.environ.get("VIDEO_FPS", "60" if platform.startswith("youtube") else "30"))
        self.vbitrate = int(os.environ.get("VIDEO_BITRATE_KBPS", "8000" if platform.startswith("youtube") else "6000"))
        self.bufsize = int(os.environ.get("VIDEO_BUFSIZE_KBPS", str(self.vbitrate * 2)))
        self.vprofile = os.environ.get("VIDEO_PROFILE", "main").strip() or "main"
        self.vpreset = os.environ.get("VIDEO_PRESET", "superfast").strip() or "superfast"
        self.udp_port = int(os.environ.get("VIDEO_UDP_PORT", "19000"))
        self.sender = None
        self.sender_log = None
        self.running = True
        self.current_signature = None
        self.current_url = None
        self.current_file = None
        self.switches = 0

    def desired(self):
        data = read_json(self.desired_path, {}) or {}
        url = str(data.get("loop_url") or self.base_loop_url).strip() or self.base_loop_url
        revision = str(data.get("visual_revision") or "0")
        return data, url, revision

    def write_health(self, status, loop_url=None, extra=None):
        payload = {
            "platform": self.platform,
            "status": status,
            "loop_url": loop_url or self.current_url or self.base_loop_url,
            "fps": self.fps,
            "video_bitrate_kbps": self.vbitrate,
            "udp_port": self.udp_port,
            "switches": self.switches,
            "updated_at": iso_now(),
        }
        if self.sender and self.sender.poll() is None:
            payload["sender_pid"] = self.sender.pid
        if self.current_file:
            payload["prepared_file"] = str(self.current_file)
        if extra:
            payload.update(extra)
        atomic_json(self.health_path, payload)

    def probe_video(self, path):
        probe = subprocess.run(
            [
                "ffprobe", "-v", "error", "-select_streams", "v:0",
                "-show_entries", "stream=codec_type", "-of", "default=nw=1:nk=1", str(path),
            ],
            capture_output=True,
            text=True,
        )
        return probe.returncode == 0 and "video" in probe.stdout

    def cache_path(self, url):
        token = hashlib.sha256(
            f"{url}|{self.fps}|{self.vbitrate}|{self.bufsize}|{self.vprofile}|{self.vpreset}|v2".encode()
        ).hexdigest()[:24]
        return self.cache_dir / f"{token}.mp4"

    def prepare(self, loop_url):
        if not loop_url:
            raise RuntimeError("loop_url is missing")
        target = self.cache_path(loop_url)
        if target.exists() and target.stat().st_size > 1024:
            if self.probe_video(target):
                return target
            target.unlink(missing_ok=True)

        self.write_health("preparing", loop_url)
        source = self.cache_dir / (target.stem + ".source")
        temp = self.cache_dir / (target.stem + ".tmp.mp4")
        source.unlink(missing_ok=True)
        temp.unlink(missing_ok=True)

        if loop_url.startswith("file://"):
            local_path = pathlib.Path(urllib.parse.urlparse(loop_url).path)
            if not local_path.exists():
                raise RuntimeError(f"local visual not found: {local_path}")
            shutil.copyfile(local_path, source)
        else:
            req = urllib.request.Request(loop_url, headers={"User-Agent": f"MediaForge-Visual-{self.platform}"})
            with urllib.request.urlopen(req, timeout=180) as response, open(source, "wb") as fh:
                while self.running:
                    chunk = response.read(1024 * 1024)
                    if not chunk:
                        break
                    fh.write(chunk)
        if not self.running:
            raise RuntimeError("visual engine stopping")

        source_is_video = self.probe_video(source)
        cmd = ["ffmpeg", "-hide_banner", "-loglevel", "warning", "-nostdin", "-y"]
        if not source_is_video:
            cmd += ["-loop", "1"]
        cmd += ["-i", str(source)]
        if not source_is_video:
            cmd += ["-t", "12"]
        gop = self.fps * 2
        cmd += [
            "-vf",
            f"scale=1920:1080:force_original_aspect_ratio=decrease:flags=lanczos,pad=1920:1080:(ow-iw)/2:(oh-ih)/2,setsar=1,fps={self.fps},format=yuv420p",
            "-an",
            "-c:v", "libx264",
            "-preset", self.vpreset,
            "-tune", "zerolatency",
            "-profile:v", self.vprofile,
            "-bf", "0",
            "-b:v", f"{self.vbitrate}k",
            "-minrate", f"{self.vbitrate}k",
            "-maxrate", f"{self.vbitrate}k",
            "-bufsize", f"{self.bufsize}k",
            "-g", str(gop),
            "-keyint_min", str(gop),
            "-sc_threshold", "0",
            "-x264-params", "nal-hrd=cbr:force-cfr=1",
            "-movflags", "+faststart",
            str(temp),
        ]
        subprocess.run(cmd, check=True)
        temp.replace(target)
        source.unlink(missing_ok=True)
        self.prune_cache(keep=4)
        return target

    def prune_cache(self, keep=4):
        files = sorted(
            [p for p in self.cache_dir.glob("*.mp4") if p.is_file()],
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        for path in files[keep:]:
            try:
                path.unlink()
            except Exception:
                pass

    def sync_ui_test_text(self):
        if self.platform != "youtube-ui-test":
            return
        now = read_json(self.state / "now-playing.json", {}) or {}
        title = str(now.get("title") or now.get("name") or "PETER LOFI").strip().upper()
        wrapped = textwrap.wrap(title, width=18)[:2] or ["PETER LOFI"]
        (self.state / "now-playing.txt").write_text("\n".join(wrapped) + "\n", encoding="utf-8")
        message = self.state / "message.txt"
        if not message.exists():
            message.write_text("HELLO GYS\n", encoding="utf-8")

    def stop_sender(self):
        proc = self.sender
        self.sender = None
        if proc and proc.poll() is None:
            try:
                proc.send_signal(signal.SIGINT)
                proc.wait(timeout=1.5)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
        if self.sender_log is not None:
            try:
                self.sender_log.close()
            except Exception:
                pass
            self.sender_log = None

    def start_sender(self, path, loop_url):
        self.stop_sender()
        target = f"udp://127.0.0.1:{self.udp_port}?pkt_size=1316"
        if self.platform == "youtube-ui-test":
            self.sync_ui_test_text()
            assets = pathlib.Path("/state/ui-test-assets")
            header = str(assets / "latest-subscriptions-frame.png")
            web = str(assets / "spider-web.png")
            icon = str(assets / "subscriber-icon.png")
            font = str(assets / "superstar.ttf")
            symbol_font = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
            title_file = str(self.state / "now-playing.txt")
            message_file = str(self.state / "message.txt")
            demo_enable = "between(mod(t\\,60)\\,45\\,52)"

            # HTML-like layer model:
            # video -> spider-web image -> SVG frame -> SVG subscriber icons ->
            # live text -> transient 20% black message layer -> live message text.
            # Text never lives inside the SVG assets.
            filters = (
                f"[2:v]scale=430:-1[web];"
                f"[3:v]split=3[subicon1][subicon2][subicon3];"
                f"[0:v][web]overlay=0:0[webbed];"
                f"[webbed][1:v]overlay=59:54[head];"
                f"[head][subicon1]overlay=84:240[s1];"
                f"[s1][subicon2]overlay=84:275[s2];"
                f"[s2][subicon3]overlay=84:310[base];"

                f"[base]drawtext=fontfile={font}:text='LATEST SUBSCRIPTIONS\\:':"
                f"fontcolor=white:fontsize=29:x=59+(448-text_w)/2:y=99[latest];"

                f"[latest]drawtext=fontfile={font}:text='@GUILHERMEODSGN':"
                f"fontcolor=white:fontsize=25:x=113:y=241[sub1];"
                f"[sub1]drawtext=fontfile={font}:text='@PETERLOFI':"
                f"fontcolor=white:fontsize=25:x=113:y=276[sub2];"
                f"[sub2]drawtext=fontfile={font}:text='@PETERLOFI':"
                f"fontcolor=white:fontsize=25:x=113:y=311[subs3];"

                f"[subs3]drawtext=fontfile={symbol_font}:text='♫':"
                f"fontcolor=black:fontsize=31:x=110:y=866[note];"
                f"[note]drawtext=fontfile={font}:text='CURRENT MUSIC\\:':"
                f"fontcolor=black:fontsize=34:x=144:y=869[label];"
                f"[label]drawtext=fontfile={font}:textfile={title_file}:reload=1:"
                f"fontcolor=black:fontsize=49:line_spacing=-8:"
                f"x=110+(302-text_w)/2:y=917[title];"

                f"[title]drawtext=fontfile={font}:text='VOTE TO CHANGE A SONG.':"
                f"fontcolor=white:fontsize=34:x=1506:y=1020[vote];"

                # Paid/user message state: one plain black alpha layer over the
                # whole video, no blend mode / multiply / side mask.
                f"[vote]drawbox=x=0:y=0:w=iw:h=ih:color=black@0.20:t=fill:"
                f"enable='{demo_enable}'[messagebg];"
                f"[messagebg]drawtext=fontfile={font}:textfile={message_file}:reload=1:"
                f"fontcolor=white:fontsize=175:x=(w-text_w)/2:y=(h-text_h)/2:"
                f"enable='{demo_enable}'[v]"
            )
            gop = self.fps * 2
            cmd = [
                "ffmpeg", "-hide_banner", "-loglevel", "warning", "-nostdin",
                "-re", "-stream_loop", "-1", "-i", str(path),
                "-loop", "1", "-i", header,
                "-loop", "1", "-i", web,
                "-loop", "1", "-i", icon,
                "-filter_complex", filters,
                "-map", "[v]", "-an",
                "-c:v", "libx264", "-preset", self.vpreset, "-tune", "zerolatency",
                "-profile:v", self.vprofile, "-bf", "0",
                "-b:v", f"{self.vbitrate}k", "-minrate", f"{self.vbitrate}k",
                "-maxrate", f"{self.vbitrate}k", "-bufsize", f"{self.bufsize}k",
                "-g", str(gop), "-keyint_min", str(gop), "-sc_threshold", "0",
                "-pix_fmt", "yuv420p",
                "-x264-params", "nal-hrd=cbr:force-cfr=1",
                "-muxdelay", "0", "-muxpreload", "0",
                "-f", "mpegts", target,
            ]
        else:
            cmd = [
                "ffmpeg", "-hide_banner", "-loglevel", "warning", "-nostdin",
                "-re", "-stream_loop", "-1", "-i", str(path),
                "-map", "0:v:0", "-an", "-c:v", "copy",
                "-bsf:v", "h264_mp4toannexb",
                "-muxdelay", "0", "-muxpreload", "0",
                "-f", "mpegts", target,
            ]
        self.sender_log = open(self.state / "visual-ffmpeg.log", "ab", buffering=0)
        self.sender = subprocess.Popen(cmd, stdout=self.sender_log, stderr=self.sender_log)
        time.sleep(0.25)
        if self.sender.poll() is not None:
            raise RuntimeError(f"visual sender exited with code {self.sender.returncode}")
        self.current_file = path
        self.current_url = loop_url
        self.write_health("streaming", loop_url)

    def run(self):
        while self.running:
            self.sync_ui_test_text()
            desired, loop_url, revision = self.desired()
            if str(desired.get("desired") or "live").lower() in {"stopped", "stop", "offline"}:
                self.stop_sender()
                self.write_health("stopped", loop_url)
                time.sleep(1)
                continue

            signature = (loop_url, revision)
            sender_ok = self.sender and self.sender.poll() is None
            if signature != self.current_signature:
                try:
                    # Preparation happens while the old sender keeps streaming.
                    prepared = self.prepare(loop_url)
                    if not self.running:
                        break
                    self.start_sender(prepared, loop_url)
                    if self.current_signature is not None:
                        self.switches += 1
                    self.current_signature = signature
                    self.write_health("streaming", loop_url, {"visual_revision": revision})
                except Exception as exc:
                    self.write_health("error", loop_url, {"error": str(exc)[:500]})
                    time.sleep(2)
                    continue
            elif not sender_ok:
                try:
                    prepared = self.current_file if self.current_file and self.current_file.exists() else self.prepare(loop_url)
                    self.start_sender(prepared, loop_url)
                    self.write_health("streaming", loop_url, {"recovered": True, "visual_revision": revision})
                except Exception as exc:
                    self.write_health("error", loop_url, {"error": str(exc)[:500]})
                    time.sleep(2)
                    continue
            else:
                self.write_health("streaming", loop_url, {"visual_revision": revision})
            time.sleep(0.5)

        self.stop_sender()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--platform", required=True, choices=["kick", "twitch", "youtube-deep-house", "youtube-rainy", "youtube-ui-test"])
    ap.add_argument("--state-dir", default="/state")
    args = ap.parse_args()
    engine = VisualEngine(args.platform, args.state_dir)

    def stop(*_):
        engine.running = False
        engine.stop_sender()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    engine.run()


if __name__ == "__main__":
    main()
