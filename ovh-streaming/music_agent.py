#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import json
import math
import mimetypes
import os
import pathlib
import re
import shutil
import subprocess
import tempfile
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone

API=os.environ.get("MEDIAFORGE_API_URL","http://127.0.0.1:8790").rstrip("/")
AGENT_TOKEN=os.environ.get("MEDIAFORGE_AGENT_TOKEN","").strip()
REPO=pathlib.Path(os.environ.get("MEDIAFORGE_REPO","/home/ubuntu/theofficemusic"))
POLL=max(10,int(os.environ.get("MEDIAFORGE_MUSIC_POLL_SECONDS","15")))
KAGGLE_USERNAME=os.environ.get("KAGGLE_USERNAME","").strip()
KAGGLE_API_TOKEN=os.environ.get("KAGGLE_API_TOKEN","").strip()
HF_TOKEN=os.environ.get("HF_TOKEN","").strip()
STABLE_AUDIO_VENDOR=pathlib.Path(os.environ.get("MEDIAFORGE_STABLE_AUDIO_VENDOR","/opt/mediaforge-vendor/stable-audio-3"))
SMALL_KERNEL_SLUG="the-office-music-generator-small"
SECRET_DATASET_SLUG="the-office-music-secrets"


def now():
    return datetime.now(timezone.utc).isoformat()


def headers(extra=None):
    h={"User-Agent":"MediaForge-OVH-Music-Agent","x-ovh-agent-token":AGENT_TOKEN}
    if extra:
        h.update(extra)
    return h


def request_json(path,method="GET",body=None,timeout=60):
    data=None
    h=headers()
    if body is not None:
        data=json.dumps(body).encode()
        h["content-type"]="application/json"
    req=urllib.request.Request(API+path,data=data,method=method,headers=h)
    with urllib.request.urlopen(req,timeout=timeout) as r:
        return json.load(r)


def ack(job_id,status="running",progress=0,phase="",error="",result=None):
    payload={"id":job_id,"status":status,"progress":progress,"phase":phase,"error":error}
    if result is not None:
        payload["result"]=result
    return request_json("/api/ovh/agent/music-job-ack","POST",payload,timeout=90)


def run(cmd,cwd=None,env=None,capture=False):
    print("$"," ".join(map(str,cmd)),flush=True)
    if capture:
        return subprocess.check_output(cmd,cwd=cwd,env=env,text=True,stderr=subprocess.STDOUT)
    subprocess.run(cmd,cwd=cwd,env=env,check=True)
    return ""


def kaggle_env():
    env=os.environ.copy()
    env["KAGGLE_USERNAME"]=KAGGLE_USERNAME
    env["KAGGLE_API_TOKEN"]=KAGGLE_API_TOKEN
    return env


def prepare_secret_dataset():
    with tempfile.TemporaryDirectory(prefix="mediaforge-hf-") as td:
        d=pathlib.Path(td)
        (d/"hf_token.txt").write_text(HF_TOKEN,encoding="utf-8")
        os.chmod(d/"hf_token.txt",0o600)
        (d/"dataset-metadata.json").write_text(json.dumps({
            "title":"The Office Music Secrets",
            "id":f"{KAGGLE_USERNAME}/{SECRET_DATASET_SLUG}",
            "licenses":[{"name":"other"}],
            "description":"Private MediaForge credential input for the OVH-orchestrated generator."
        },indent=2),encoding="utf-8")
        dataset=f"{KAGGLE_USERNAME}/{SECRET_DATASET_SLUG}"
        env=kaggle_env()
        try:
            run(["kaggle","datasets","status",dataset],env=env,capture=True)
            run(["kaggle","datasets","version","-p",str(d),"-m","Refresh HF token","-q"],env=env)
        except Exception:
            run(["kaggle","datasets","create","-p",str(d),"--private","-q"],env=env)


def copy_bundle(temp:pathlib.Path):
    bundle=temp/"bundle"
    (bundle/"config").mkdir(parents=True)
    (bundle/"scripts").mkdir(parents=True)
    (bundle/"src").mkdir(parents=True)
    shutil.copy2(REPO/"config"/"peter_lofi_series.json",bundle/"config"/"peter_lofi_series.json")
    shutil.copy2(REPO/"scripts"/"bootstrap_kaggle_ovh.sh",bundle/"scripts"/"bootstrap_kaggle_ovh.sh")
    shutil.copy2(REPO/"src"/"generate_tracks.py",bundle/"src"/"generate_tracks.py")
    shutil.copy2(REPO/"src"/"prompt_engine.py",bundle/"src"/"prompt_engine.py")
    if not (STABLE_AUDIO_VENDOR/"pyproject.toml").is_file():
        raise RuntimeError(f"Stable Audio vendor cache missing: {STABLE_AUDIO_VENDOR}")
    shutil.copytree(STABLE_AUDIO_VENDOR,bundle/"vendor"/"stable-audio-3",dirs_exist_ok=True,ignore=shutil.ignore_patterns(".git",".venv","__pycache__"))


def prepare_kernel(temp:pathlib.Path,job:dict):
    duration=int(job["duration_minutes"])
    raw_count=math.ceil(duration*60/120)
    runner=(REPO/"kaggle"/"runner_small_ovh.py").read_text(encoding="utf-8")
    runner=re.sub(r"TRACK_COUNT = \d+",f"TRACK_COUNT = {raw_count}",runner,count=1)
    runner=re.sub(r'REQUEST_ID = "[^"]*"',f'REQUEST_ID = "{job["id"]}"',runner,count=1)
    runner=re.sub(r'SERIES_KEY = "[^"]*"',f'SERIES_KEY = "{job["series_key"]}"',runner,count=1)
    (temp/"runner.py").write_text(runner,encoding="utf-8")
    copy_bundle(temp)
    meta={
        "id":f"{KAGGLE_USERNAME}/{SMALL_KERNEL_SLUG}",
        "title":"The Office Music Generator Small",
        "code_file":"runner.py",
        "language":"python",
        "kernel_type":"script",
        "is_private":True,
        "enable_gpu":True,
        "enable_internet":True,
        "dataset_sources":[f"{KAGGLE_USERNAME}/{SECRET_DATASET_SLUG}"],
        "competition_sources":[],
        "kernel_sources":[]
    }
    (temp/"kernel-metadata.json").write_text(json.dumps(meta,indent=2),encoding="utf-8")


def wait_kernel(job_id):
    kernel=f"{KAGGLE_USERNAME}/{SMALL_KERNEL_SLUG}"
    env=kaggle_env()
    for i in range(1,151):
        status=run(["kaggle","kernels","status",kernel],env=env,capture=True)
        print(status.strip(),flush=True)
        lower=status.lower()
        if "complete" in lower:
            ack(job_id,"running",80,"Geração concluída no Kaggle; baixando áudio")
            return
        if any(x in lower for x in ("error","failed","cancel")):
            try:
                print(run(["kaggle","kernels","logs",kernel],env=env,capture=True)[-10000:],flush=True)
            except Exception:
                pass
            raise RuntimeError("Kaggle generation failed")
        progress=min(75,20+int(i/150*55))
        if i==1 or i%5==0:
            ack(job_id,"running",progress,"Gerando músicas no Kaggle")
        time.sleep(60)
    raise RuntimeError("Kaggle generation timed out")


def sha256(path:pathlib.Path):
    h=hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda:f.read(1024*1024),b""):
            h.update(chunk)
    return h.hexdigest()


def duration_seconds(path:pathlib.Path):
    out=run(["ffprobe","-v","error","-show_entries","format=duration","-of","default=nw=1:nk=1",str(path)],capture=True).strip()
    return float(out)


def upload_asset(path:pathlib.Path,title:str,asset_type="audio"):
    mime=mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    qs=urllib.parse.urlencode({"name":path.name,"title":title,"asset_type":asset_type})
    req=urllib.request.Request(
        API+"/api/ovh/agent/assets?"+qs,
        data=path.open("rb"),
        method="PUT",
        headers=headers({"content-type":mime,"content-length":str(path.stat().st_size)}),
    )
    with urllib.request.urlopen(req,timeout=1800) as r:
        return json.load(r)["asset"]


def build_master(job:dict,generated:pathlib.Path,work:pathlib.Path):
    target=int(job["duration_minutes"])*60
    build=work/"build";build.mkdir(parents=True,exist_ok=True)
    run([
        "python3",str(REPO/"scripts"/"validate_fresh_audio.py"),
        "--generated-dir",str(generated),
        "--target-seconds",str(target),
        "--concat-file",str(build/"concat.txt"),
        "--manifest-file",str(build/"tracks.json"),
    ])
    master=build/"peter-lofi-master.m4a"
    run([
        "ffmpeg","-hide_banner","-loglevel","warning","-y",
        "-f","concat","-safe","0","-i",str(build/"concat.txt"),
        "-t",str(target),"-vn","-c:a","aac","-b:a","320k","-ar","48000",str(master)
    ])
    if not master.is_file() or master.stat().st_size<1024:
        raise RuntimeError("Master audio was not produced")
    manifest=json.loads((build/"tracks.json").read_text(encoding="utf-8"))
    return master,manifest


def execute_job(job:dict):
    job_id=str(job["id"])
    for key,value in {
        "KAGGLE_USERNAME":KAGGLE_USERNAME,
        "KAGGLE_API_TOKEN":KAGGLE_API_TOKEN,
        "HF_TOKEN":HF_TOKEN,
        "MEDIAFORGE_AGENT_TOKEN":AGENT_TOKEN,
    }.items():
        if not value:
            raise RuntimeError(f"Missing operational credential on OVH: {key}")

    ack(job_id,"running",5,"Preparando credenciais e kernel na OVH")
    prepare_secret_dataset()
    ack(job_id,"running",12,"Enviando job de geração para Kaggle")

    with tempfile.TemporaryDirectory(prefix="mediaforge-music-") as td:
        work=pathlib.Path(td)
        kernel_dir=work/"kernel";kernel_dir.mkdir()
        prepare_kernel(kernel_dir,job)
        env=kaggle_env()
        run(["kaggle","kernels","push","-p",str(kernel_dir),"--timeout","7200"],env=env)
        time.sleep(45)
        wait_kernel(job_id)

        generated=work/"generated";generated.mkdir()
        kernel=f"{KAGGLE_USERNAME}/{SMALL_KERNEL_SLUG}"
        run(["kaggle","kernels","output",kernel,"-p",str(generated)],env=env)

        marker=next(generated.rglob("request_id.txt"),None)
        if not marker or marker.read_text().strip()!=job_id:
            raise RuntimeError("Kaggle output request marker mismatch")
        wavs=sorted(generated.rglob("*.wav"))
        if not wavs:
            raise RuntimeError("No fresh WAV output returned by Kaggle")

        flat=work/"flat";flat.mkdir()
        for p in wavs:
            shutil.copy2(p,flat/p.name)

        ack(job_id,"running",84,"Validando faixas e montando master na OVH")
        master,manifest=build_master(job,flat,work)

        ack(job_id,"running",90,"Salvando faixas no armazenamento local da OVH")
        uploaded=[]
        by_name={p.name:p for p in flat.glob("*.wav")}
        for i,item in enumerate(manifest,1):
            p=by_name.get(item["filename"])
            if not p:
                raise RuntimeError("Manifest references missing WAV: "+item["filename"])
            title=str(item.get("title") or p.stem)
            asset=upload_asset(p,title,"audio")
            uploaded.append({
                **item,
                "id":f"generated-{job_id[:8]}-{i:02d}",
                "position":i,
                "url":asset["runtime_url"],
                "public_url":asset["public_url"],
                "asset_id":asset["id"],
                "sha256":item.get("sha256") or sha256(p),
            })

        master_asset=upload_asset(master,str(job.get("playlist_name") or "Peter Lofi Master"),"audio")
        slug=re.sub(r"[^a-z0-9]+","-",str(job.get("playlist_name") or "").lower()).strip("-")[:48] or str(job.get("series_key") or "custom")
        result={
            "status":"completed",
            "progress":100,
            "phase":"Concluída e catalogada na OVH",
            "request_id":job_id,
            "library_key":f"generated-{slug}-{job_id[:8]}",
            "series_key":job["series_key"],
            "playlist_name":job["playlist_name"],
            "duration_minutes":int(job["duration_minutes"]),
            "master_audio_url":master_asset["public_url"],
            "master_runtime_url":master_asset["runtime_url"],
            "master_asset_id":master_asset["id"],
            "tracks":uploaded,
            "execution":"ovh+kaggle",
            "github_actions":False,
            "completed_at":now(),
        }
        ack(job_id,"completed",100,"Concluída e catalogada na OVH",result=result)


def main():
    if not AGENT_TOKEN:
        raise SystemExit("MEDIAFORGE_AGENT_TOKEN is required")
    print("MediaForge OVH music agent online.",flush=True)
    while True:
        try:
            payload=request_json("/api/ovh/agent/music-jobs?limit=1")
            jobs=payload.get("jobs") or []
            if jobs:
                job=jobs[0]
                try:
                    execute_job(job)
                except Exception as exc:
                    print("music job failed:",repr(exc),flush=True)
                    try:
                        ack(str(job.get("id") or ""),"failed",100,"Falha na geração OVH",error=str(exc)[:1800])
                    except Exception:
                        pass
            else:
                time.sleep(POLL)
        except Exception as exc:
            print("music agent loop:",repr(exc),flush=True)
            time.sleep(POLL)


if __name__=="__main__":
    main()
