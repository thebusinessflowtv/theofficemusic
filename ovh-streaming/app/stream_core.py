#!/usr/bin/env python3
import argparse
import json
import os
import pathlib
import shutil
import signal
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone

def iso_now():
    return datetime.now(timezone.utc).isoformat().replace("+00:00","Z")

def atomic_json(path,payload):
    path.parent.mkdir(parents=True,exist_ok=True)
    tmp=path.with_suffix(path.suffix+".tmp")
    tmp.write_text(json.dumps(payload,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    tmp.replace(path)

def read_json(path,default=None):
    try:return json.loads(path.read_text(encoding="utf-8"))
    except Exception:return default

class StreamCore:
    def __init__(self,platform):
        self.platform=platform
        self.state=pathlib.Path("/state")/platform
        self.state.mkdir(parents=True,exist_ok=True)
        self.health_path=self.state/"health.json"
        self.desired_path=self.state/"desired.json"
        self.runtime_playlist=self.state/"playlist.json"
        self.base_playlist=os.environ.get("PLAYLIST_FILE","/config/gaming.json")
        self.stream_url=os.environ.get("STREAM_URL","").rstrip("/")
        self.stream_key=os.environ.get("STREAM_KEY","").strip()
        self.base_loop_url=os.environ.get("LOOP_URL","").strip()
        self.loop_url=self.base_loop_url
        self.fps=int(os.environ.get("VIDEO_FPS","60" if platform.startswith("youtube") else "30"))
        self.vbitrate=int(os.environ.get("VIDEO_BITRATE_KBPS","8000" if platform.startswith("youtube") else "6000"))
        self.bufsize=int(os.environ.get("VIDEO_BUFSIZE_KBPS",str(self.vbitrate*2)))
        self.abitrate=int(os.environ.get("AUDIO_BITRATE_KBPS","160"))
        self.vprofile=os.environ.get("VIDEO_PROFILE","main").strip() or "main"
        self.vpreset=os.environ.get("VIDEO_PRESET","superfast").strip() or "superfast"
        self.video_copy_mode=os.environ.get("VIDEO_COPY_MODE","0").strip().lower() in {"1","true","yes","on"}
        self.encoder=self.feeder=None
        self.fd=None
        self.ffmpeg_log=None
        self.running=True
        self.restarts=0
        self.current_generation=None
        self.current_loop_url=None
        self.current_loop=None
        self.connected_since=None
        self.bootstrap_runtime()

    def bootstrap_runtime(self):
        if not self.runtime_playlist.exists():
            src=pathlib.Path(self.base_playlist)
            if src.exists(): shutil.copyfile(src,self.runtime_playlist)
        if not self.desired_path.exists():
            atomic_json(self.desired_path,{
                "desired":"live",
                "runtime":"ovh",
                "runtime_slot":self.platform,
                "session_id":os.environ.get("BOOTSTRAP_SESSION_ID",""),
                "title":os.environ.get("BOOTSTRAP_TITLE",""),
                "loop_url":self.base_loop_url,
                "generation":1,
                "updated_at":iso_now(),
            })

    def validate(self):
        if not self.stream_url: raise RuntimeError("STREAM_URL is missing")
        if not self.stream_key: raise RuntimeError("STREAM_KEY is missing")
        if not self.base_loop_url: raise RuntimeError("LOOP_URL is missing")
        if not self.runtime_playlist.exists(): raise RuntimeError(f"playlist file not found: {self.runtime_playlist}")

    def desired(self):
        d=read_json(self.desired_path,{}) or {}
        d.setdefault("desired","live")
        d.setdefault("loop_url",self.base_loop_url)
        d.setdefault("generation",1)
        d.setdefault("runtime","ovh")
        d.setdefault("runtime_slot",self.platform)
        return d

    def download_visual(self,loop_url):
        source=self.state/"visual-source"
        loop=self.state/"loop.mp4"
        req=urllib.request.Request(loop_url,headers={"User-Agent":f"MediaForge-OVH-{self.platform}"})
        with urllib.request.urlopen(req,timeout=180) as r,open(source,"wb") as f:
            while True:
                chunk=r.read(1024*1024)
                if not chunk: break
                f.write(chunk)
        probe=subprocess.run(["ffprobe","-v","error","-select_streams","v:0","-show_entries","stream=codec_type","-of","default=nw=1:nk=1",str(source)],capture_output=True,text=True)
        source_is_video=probe.returncode==0 and "video" in probe.stdout
        if self.video_copy_mode:
            args=["ffmpeg","-hide_banner","-loglevel","warning","-y"]
            if not source_is_video: args+=["-loop","1"]
            args+=["-i",str(source)]
            if not source_is_video: args+=["-t","12"]
            args += [
                "-vf",f"scale=1920:1080:force_original_aspect_ratio=decrease:flags=lanczos,pad=1920:1080:(ow-iw)/2:(oh-ih)/2,setsar=1,fps={self.fps},format=yuv420p",
                "-an","-c:v","libx264","-preset",self.vpreset,"-profile:v",self.vprofile,
                "-b:v",f"{self.vbitrate}k","-minrate",f"{self.vbitrate}k","-maxrate",f"{self.vbitrate}k",
                "-bufsize",f"{self.bufsize}k","-g",str(self.fps*2),"-keyint_min",str(self.fps*2),"-sc_threshold","0",
                "-x264-params","nal-hrd=cbr:force-cfr=1",str(loop)
            ]
            subprocess.run(args,check=True)
        elif source_is_video:
            loop.write_bytes(source.read_bytes())
        else:
            subprocess.run([
                "ffmpeg","-hide_banner","-loglevel","warning","-y","-loop","1","-i",str(source),"-t","12",
                "-vf",f"scale=1920:1080:force_original_aspect_ratio=decrease:flags=lanczos,pad=1920:1080:(ow-iw)/2:(oh-ih)/2,setsar=1,fps={self.fps},format=yuv420p",
                "-an","-c:v","libx264","-preset",self.vpreset,"-profile:v",self.vprofile,
                "-b:v",f"{self.vbitrate}k","-minrate",f"{self.vbitrate}k","-maxrate",f"{self.vbitrate}k",
                "-bufsize",f"{self.bufsize}k","-g",str(self.fps*2),"-keyint_min",str(self.fps*2),"-sc_threshold","0",
                "-x264-params","nal-hrd=cbr:force-cfr=1",str(loop)
            ],check=True)
        return loop

    def target(self):
        base=self.stream_url.rstrip("/")
        if self.platform=="kick":
            parsed=urllib.parse.urlparse(base)
            if parsed.scheme=="rtmps" and "global-contribute.live-video.net" in (parsed.hostname or ""):
                netloc=parsed.netloc if parsed.port is not None else f"{parsed.hostname}:443"
                path=parsed.path.rstrip("/") or "/app"
                base=urllib.parse.urlunparse((parsed.scheme,netloc,path,"","","")).rstrip("/")
        return f"{base}/{self.stream_key.lstrip('/')}"

    def cleanup(self):
        for proc in (self.encoder,self.feeder):
            if proc and proc.poll() is None:
                try: proc.send_signal(signal.SIGINT)
                except Exception: pass
        time.sleep(.4)
        for proc in (self.encoder,self.feeder):
            if proc and proc.poll() is None:
                try: proc.kill()
                except Exception: pass
        if self.fd is not None:
            try: os.close(self.fd)
            except Exception: pass
        self.encoder=self.feeder=None
        self.fd=None
        if self.ffmpeg_log is not None:
            try:self.ffmpeg_log.close()
            except Exception:pass
            self.ffmpeg_log=None

    def start(self,loop,desired):
        self.cleanup()
        fifo=self.state/"audio.pcm"
        try:fifo.unlink()
        except FileNotFoundError:pass
        os.mkfifo(fifo)
        self.fd=os.open(fifo,os.O_RDWR)
        self.feeder=subprocess.Popen([
            sys.executable,"/app/audio_engine.py","--platform",self.platform,
            "--playlist-file",str(self.runtime_playlist),"--state-dir","/state"
        ],stdout=self.fd,stderr=subprocess.DEVNULL)
        if self.video_copy_mode:
            cmd=["ffmpeg","-hide_banner","-loglevel","warning","-re","-stream_loop","-1","-i",str(loop),
                 "-thread_queue_size","1024","-f","s16le","-ar","48000","-ac","2","-i",str(fifo),
                 "-map","0:v:0","-map","1:a:0","-c:v","copy",
                 "-c:a","aac","-b:a",f"{self.abitrate}k","-ar","48000","-ac","2",
                 "-flvflags","no_duration_filesize","-f","flv",self.target()]
        else:
            gop=self.fps*2
            cmd=["ffmpeg","-hide_banner","-loglevel","warning","-re","-stream_loop","-1","-i",str(loop),
                 "-thread_queue_size","1024","-f","s16le","-ar","48000","-ac","2","-i",str(fifo),
                 "-map","0:v:0","-map","1:a:0",
                 "-vf",f"scale=1920:1080:force_original_aspect_ratio=decrease:flags=lanczos,pad=1920:1080:(ow-iw)/2:(oh-ih)/2,setsar=1,fps={self.fps},format=yuv420p",
                 "-r",str(self.fps),"-s:v","1920x1080","-pix_fmt","yuv420p",
                 "-c:v","libx264","-preset",self.vpreset,"-tune","zerolatency","-profile:v",self.vprofile,
                 "-b:v",f"{self.vbitrate}k","-minrate",f"{self.vbitrate}k","-maxrate",f"{self.vbitrate}k",
                 "-bufsize",f"{self.bufsize}k","-g",str(gop),"-keyint_min",str(gop),"-sc_threshold","0",
                 "-x264-params","nal-hrd=cbr:force-cfr=1",
                 "-c:a","aac","-b:a",f"{self.abitrate}k","-ar","48000","-ac","2",
                 "-flvflags","no_duration_filesize","-f","flv",self.target()]
        self.ffmpeg_log=open(self.state/"ffmpeg.log","ab",buffering=0)
        self.encoder=subprocess.Popen(cmd,stdout=self.ffmpeg_log,stderr=self.ffmpeg_log)
        self.connected_since=time.time()
        self.write_health("starting",desired)

    def write_health(self,status,desired,extra=None):
        payload={
            "platform":self.platform,"runtime":"ovh","runtime_slot":self.platform,
            "session_id":desired.get("session_id") or "","title":desired.get("title") or "",
            "status":status,"fps":self.fps,"video_bitrate_kbps":self.vbitrate,
            "restarts":self.restarts,"updated_at":iso_now(),
            "loop_url":desired.get("loop_url") or self.base_loop_url,
        }
        if self.encoder and self.encoder.poll() is None: payload["encoder_pid"]=self.encoder.pid
        if self.feeder and self.feeder.poll() is None: payload["audio_pid"]=self.feeder.pid
        if extra:payload.update(extra)
        atomic_json(self.health_path,payload)

    def monitor(self):
        while self.running:
            d=self.desired()
            desired_state=str(d.get("desired") or "live").lower()
            generation=str(d.get("generation") or "1")
            loop_url=str(d.get("loop_url") or self.base_loop_url).strip() or self.base_loop_url
            if desired_state in {"stopped","stop","offline"}:
                if self.encoder or self.feeder:self.cleanup()
                self.current_generation=generation
                self.write_health("stopped",d)
                time.sleep(2)
                continue

            enc_ok=self.encoder and self.encoder.poll() is None
            feed_ok=self.feeder and self.feeder.poll() is None
            config_changed=(self.current_generation!=generation or self.current_loop_url!=loop_url)
            if config_changed or not enc_ok or not feed_ok:
                if not config_changed and self.current_generation is not None:self.restarts+=1
                try:
                    if config_changed or self.current_loop is None:
                        self.loop_url=loop_url
                        self.current_loop=self.download_visual(loop_url)
                        self.current_loop_url=loop_url
                    self.start(self.current_loop,d)
                    self.current_generation=generation
                except Exception as exc:
                    self.restarts+=1
                    self.cleanup()
                    self.write_health("restarting",d,{"error":str(exc)[:500]})
                    time.sleep(min(20,2+self.restarts))
                    continue

            status="live" if self.connected_since and time.time()-self.connected_since>=30 else "starting"
            self.write_health(status,d)
            time.sleep(5)

def main():
    ap=argparse.ArgumentParser()
    ap.add_argument("--platform",required=True,choices=["kick","twitch","youtube-deep-house","youtube-rainy"])
    args=ap.parse_args()
    core=StreamCore(args.platform)
    def stop(*_):
        core.running=False
        core.cleanup()
    signal.signal(signal.SIGTERM,stop);signal.signal(signal.SIGINT,stop)
    core.validate()
    try:core.monitor()
    finally:core.cleanup()

if __name__=="__main__":main()
