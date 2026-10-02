#!/usr/bin/env python3
"""MediaForge Twitch live segment V1 — continuity-first GitHub runner.

Uses the Twitch primary stream key from GitHub Secrets and the same MediaForge
audio/visual assets used by the Gaming station. Successor runners prewarm before
GitHub's hosted-runner ceiling and take over only after the predecessor releases
the single Twitch stream key.
"""
import base64
import datetime as dt
import json
import os
import pathlib
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request

REPO=os.environ["GITHUB_REPOSITORY"]
TOKEN=os.environ["GH_TOKEN"]
RUN_ID=int(os.environ["GITHUB_RUN_ID"])
RUN_URL=f"https://github.com/{REPO}/actions/runs/{RUN_ID}"
SESSION_ID=os.environ["SESSION_ID"]
TRACK_URLS_B64=os.environ["TRACK_URLS_B64"]
DURATION_MINUTES=int(os.environ.get("DURATION_MINUTES","0"))
TITLE=os.environ.get("TITLE","Peter Lofi Gaming Radio")
DESCRIPTION=os.environ.get("DESCRIPTION","")
LOOP_URL=os.environ.get("LOOP_URL","")
SEGMENT_INDEX=int(os.environ.get("SEGMENT_INDEX","1"))
STREAM_KEY=os.environ.get("TWITCH_STREAM_KEY","").strip()
INGEST=os.environ.get("TWITCH_INGEST_URL","rtmp://live.twitch.tv/app").rstrip("/")
BUILD=pathlib.Path("build-twitch")
RAW=f"https://raw.githubusercontent.com/{REPO}/main"

encoder=feeder=encoder_log=feeder_log=None

def iso_now():
    return dt.datetime.now(dt.timezone.utc).isoformat()

def raw_get(path):
    try:
        req=urllib.request.Request(f"{RAW}/{path}?ts={int(time.time()*1000)}",headers={"User-Agent":"MediaForge-Twitch-V1"})
        with urllib.request.urlopen(req,timeout=12) as r:return json.load(r)
    except urllib.error.HTTPError as e:
        if e.code==404:return None
        print(f"::warning::raw read HTTP {e.code} {path}",flush=True); return None
    except Exception as e:
        print(f"::warning::raw read failed {path}: {e}",flush=True); return None

def gh_request(method,path,body=None,allow_404=False):
    url=f"https://api.github.com/repos/{REPO}/{path.lstrip('/')}"
    headers={"Authorization":f"Bearer {TOKEN}","Accept":"application/vnd.github+json","X-GitHub-Api-Version":"2022-11-28","User-Agent":"MediaForge-Twitch-V1"}
    data=None
    if body is not None:
        headers["Content-Type"]="application/json"; data=json.dumps(body).encode()
    req=urllib.request.Request(url,data=data,headers=headers,method=method)
    try:
        with urllib.request.urlopen(req,timeout=30) as r:
            raw=r.read(); return json.loads(raw.decode()) if raw else {}
    except urllib.error.HTTPError as e:
        if allow_404 and e.code==404:return None
        raise RuntimeError(f"GitHub HTTP {e.code}: {e.read().decode('utf-8','replace')[-800:]}")

def gh_put(path,payload,message):
    for n in range(1,7):
        try:
            current=gh_request("GET",f"contents/{path}",allow_404=True)
            body={"message":message,"content":base64.b64encode((json.dumps(payload,ensure_ascii=False,indent=2)+"\n").encode()).decode(),"branch":"main"}
            if current and current.get("sha"):body["sha"]=current["sha"]
            gh_request("PUT",f"contents/{path}",body)
            return True
        except Exception as e:
            print(f"state write retry {n}/6 {path}: {e}",flush=True); time.sleep(min(n,5))
    return False

def download(url,out):
    if not url:return False
    for n in range(1,6):
        try:
            req=urllib.request.Request(url,headers={"User-Agent":"MediaForge-Twitch-V1"})
            with urllib.request.urlopen(req,timeout=180) as r, open(out,"wb") as f:
                while True:
                    chunk=r.read(1024*1024)
                    if not chunk:break
                    f.write(chunk)
            return pathlib.Path(out).stat().st_size>0
        except Exception as e:
            print(f"download retry {n}/5: {e}",flush=True); time.sleep(min(8,n*2))
    return False

def valid_video(path):
    return subprocess.run(["ffmpeg","-hide_banner","-v","error","-i",str(path),"-map","0:v:0","-t","1","-f","null","-"],
                          stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL).returncode==0

def prepare_loop():
    BUILD.mkdir(parents=True,exist_ok=True)
    src=BUILD/"visual-source"; loop=BUILD/"loop.mp4"
    if not (LOOP_URL and download(LOOP_URL,src) and valid_video(src)):
        raise RuntimeError("Twitch visual loop could not be downloaded/validated")
    mime=subprocess.run(["file","-b","--mime-type",str(src)],capture_output=True,text=True,check=True).stdout.strip()
    if mime.startswith("image/"):
        subprocess.run(["ffmpeg","-hide_banner","-loglevel","warning","-y","-loop","1","-i",str(src),"-t","12",
                        "-vf","scale=1920:1080:force_original_aspect_ratio=decrease:flags=lanczos,pad=1920:1080:(ow-iw)/2:(oh-ih)/2,setsar=1,fps=30,format=yuv420p",
                        "-an","-c:v","libx264","-preset","veryfast","-crf","18","-g","60","-keyint_min","60","-sc_threshold","0",str(loop)],check=True)
    else:
        loop.write_bytes(src.read_bytes())
    if not valid_video(loop):raise RuntimeError("Prepared Twitch loop is invalid")
    return loop

def target():
    return f"{INGEST}/{STREAM_KEY}"

def stop_processes():
    global encoder,feeder,encoder_log,feeder_log
    if encoder and encoder.poll() is None:
        try:encoder.send_signal(signal.SIGINT)
        except Exception:pass
    if feeder and feeder.poll() is None:
        try:feeder.terminate()
        except Exception:pass
    end=time.time()+3
    while encoder and encoder.poll() is None and time.time()<end:time.sleep(.1)
    if encoder and encoder.poll() is None:encoder.kill()
    if feeder and feeder.poll() is None:feeder.kill()
    for h in (encoder_log,feeder_log):
        try:
            if h:h.close()
        except Exception:pass
    encoder=feeder=encoder_log=feeder_log=None

def start_encoder(loop):
    global encoder,feeder,encoder_log,feeder_log
    stop_processes()
    fifo=BUILD/"audio.pcm"
    try:fifo.unlink()
    except FileNotFoundError:pass
    os.mkfifo(fifo)
    feeder_log=open(BUILD/"audio-feeder.log","ab",buffering=0)
    fd=os.open(fifo,os.O_RDWR)
    feeder=subprocess.Popen([sys.executable,"scripts/mediaforge_live_audio_feeder.py","--session-id",SESSION_ID,
                             "--fallback-b64",TRACK_URLS_B64,"--segment-index",str(SEGMENT_INDEX)],
                            stdout=fd,stderr=feeder_log)
    encoder_log=open(BUILD/"ffmpeg.log","ab",buffering=0)
    encoder=subprocess.Popen([
        "ffmpeg","-hide_banner","-loglevel","warning","-re","-stream_loop","-1","-i",str(loop),
        "-thread_queue_size","1024","-f","s16le","-ar","48000","-ac","2","-i",str(fifo),
        "-map","0:v:0","-map","1:a:0",
        "-vf","scale=1920:1080:force_original_aspect_ratio=decrease:flags=lanczos,pad=1920:1080:(ow-iw)/2:(oh-ih)/2,setsar=1,fps=30,format=yuv420p",
        "-r","30","-s:v","1920x1080","-pix_fmt","yuv420p","-c:v","libx264","-preset","veryfast","-tune","zerolatency",
        "-profile:v","high","-level:v","4.1","-b:v","6000k","-minrate","6000k","-maxrate","6000k","-bufsize","12000k",
        "-g","60","-keyint_min","60","-sc_threshold","0","-x264-params","nal-hrd=cbr:force-cfr=1",
        "-c:a","aac","-b:a","160k","-ar","48000","-ac","2","-flvflags","no_duration_filesize","-f","flv",target()
    ],stdout=encoder_log,stderr=encoder_log)
    os.close(fd)
    print(f"Twitch encoder started pid={encoder.pid}.",flush=True)

def assert_alive():
    if not encoder or encoder.poll() is not None:
        tail=""
        try:tail=(BUILD/"ffmpeg.log").read_text(errors="replace")[-3000:]
        except Exception:pass
        raise RuntimeError("Twitch FFmpeg stopped unexpectedly:\n"+tail)
    if not feeder or feeder.poll() is not None:raise RuntimeError("Twitch audio feeder stopped")

def mark(status,verified=False,error=None):
    p=f"control/twitch-live-results/{SESSION_ID}.json"
    d=raw_get(p) or {}
    d.update({"platform":"twitch","status":status,"title":TITLE,"description":DESCRIPTION,
              "session_id":SESSION_ID,"segment_index":SEGMENT_INDEX,"github_run_id":RUN_ID,
              "github_run_url":RUN_URL,"encoder_resolution":"1920x1080","encoder_fps":30,
              "encoder_bitrate_kbps":6000,"encoder_connected":status in {"starting","live"},
              "twitch_ingest_verified":verified,"updated_at":iso_now()})
    if verified:d["live_at"]=iso_now()
    if error:d["error_message"]=str(error)
    elif status in {"starting","live"}:
        d.pop("error_message",None); d.pop("failed_at",None); d.pop("completed_at",None)
    gh_put(p,d,f"peter-lofi: Twitch {status} {SESSION_ID} segment {SEGMENT_INDEX}")

def mark_ready():
    p=f"control/twitch-prewarm/{SESSION_ID}-{SEGMENT_INDEX}.json"
    payload={"ready":True,"platform":"twitch","session_id":SESSION_ID,"segment_index":SEGMENT_INDEX,
             "run_id":RUN_ID,"run_url":RUN_URL,"prepared_at":iso_now()}
    while not gh_put(p,payload,f"peter-lofi: Twitch segment ready {SESSION_ID} {SEGMENT_INDEX}"):
        time.sleep(30)

def wait_takeover():
    if SEGMENT_INDEX<=1:return
    p=f"control/twitch-takeover/{SESSION_ID}-{SEGMENT_INDEX}.json"
    print(f"Twitch segment {SEGMENT_INDEX} prewarmed; waiting for takeover.",flush=True)
    while True:
        d=raw_get(p)
        if d and d.get("takeover") is True and int(d.get("from_segment_index") or 0)==SEGMENT_INDEX-1:
            cut=float(d.get("cutover_epoch") or 0)
            while cut>time.time():time.sleep(min(.05,max(.005,cut-time.time())))
            return
        time.sleep(10)

def dispatch_next():
    remain=0 if DURATION_MINUTES==0 else max(1,DURATION_MINUTES-300)
    body={"ref":"main","inputs":{
        "session_id":SESSION_ID,"track_urls_b64":TRACK_URLS_B64,"duration_minutes":str(remain),
        "title":TITLE,"description":DESCRIPTION,"loop_url":LOOP_URL,"segment_index":str(SEGMENT_INDEX+1)
    }}
    try:
        gh_request("POST","actions/workflows/peter-lofi-twitch-live.yml/dispatches",body)
        print(f"Twitch successor {SEGMENT_INDEX+1} dispatched.",flush=True); return True
    except Exception as e:
        print(f"::warning::Twitch successor dispatch failed: {e}",flush=True); return False

def successor_ready():
    d=raw_get(f"control/twitch-prewarm/{SESSION_ID}-{SEGMENT_INDEX+1}.json")
    return bool(d and d.get("ready") is True and int(d.get("segment_index") or 0)==SEGMENT_INDEX+1)

def stop_requested():
    d=raw_get(f"control/twitch-live-stop/{SESSION_ID}.json")
    return bool(d and d.get("stop") is True)

def validate():
    if not STREAM_KEY:raise RuntimeError("TWITCH_STREAM_KEY secret is missing")
    urls=json.loads(base64.b64decode(TRACK_URLS_B64).decode())
    if not isinstance(urls,list) or not urls:raise RuntimeError("No Twitch track URLs")
    if not LOOP_URL:raise RuntimeError("Missing Twitch loop URL")

def main():
    validate()
    loop=prepare_loop()
    if SEGMENT_INDEX>1:
        mark_ready(); wait_takeover()
    start_encoder(loop)
    mark("starting",False)

    # Twitch accepts the stream key at ingest. A bad/unauthorized key normally causes
    # the RTMP publisher to terminate; surviving this window verifies transport/auth
    # without requiring Twitch API OAuth.
    for n in range(1,7):
        time.sleep(5); assert_alive()
        print(f"Twitch ingest verification {n}/6: encoder healthy.",flush=True)
    mark("live",True)

    seconds=300*60 if DURATION_MINUTES==0 else min(300,DURATION_MINUTES)*60
    deadline=int(time.time())+seconds
    prewarm=deadline-min(900,max(60,seconds//2))
    dispatched=False; last_dispatch=0; reconnects=0
    while True:
        now=int(time.time())
        if stop_requested():
            stop_processes(); mark("completed",False); return
        if not encoder or encoder.poll() is not None or not feeder or feeder.poll() is not None:
            reconnects+=1
            if reconnects>20:raise RuntimeError("Twitch reconnect limit reached")
            print(f"Twitch reconnect {reconnects}/20.",flush=True)
            start_encoder(loop); time.sleep(12); continue
        if not dispatched and now>=prewarm:
            dispatched=dispatch_next(); last_dispatch=now
        if now>=deadline:
            if not dispatched:
                dispatched=dispatch_next(); last_dispatch=now
            if successor_ready():
                cut=time.time()+20
                payload={"platform":"twitch","session_id":SESSION_ID,"from_segment_index":SEGMENT_INDEX,
                         "to_segment_index":SEGMENT_INDEX+1,"segment_index":SEGMENT_INDEX+1,
                         "takeover":True,"cutover_epoch":round(cut,6),"signaled_at":iso_now(),
                         "source_run_id":RUN_ID,"handoff_protocol":"twitch-v1-single-key"}
                if gh_put(f"control/twitch-takeover/{SESSION_ID}-{SEGMENT_INDEX+1}.json",payload,
                          f"peter-lofi: Twitch takeover {SESSION_ID} {SEGMENT_INDEX+1}"):
                    while time.time()<cut:
                        assert_alive(); time.sleep(min(.05,max(.005,cut-time.time())))
                    stop_processes(); return
            if now-last_dispatch>=180:
                dispatch_next(); last_dispatch=now
            print("Twitch successor not ready; predecessor remains online.",flush=True)
        time.sleep(5)

if __name__=="__main__":
    try:
        main()
    except Exception as exc:
        print(f"FATAL TWITCH: {exc}",file=sys.stderr,flush=True)
        try:stop_processes()
        except Exception:pass
        mark("failed",False,exc)
        raise
