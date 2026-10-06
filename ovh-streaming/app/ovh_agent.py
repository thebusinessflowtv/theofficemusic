#!/usr/bin/env python3
"""MediaForge OVH live-control agent.

Live state belongs to OVH. Cloudflare is only a control/UI transport:
- local desired/playlist/now-playing/health state is authoritative;
- local heartbeat remains frequent;
- Cloudflare status sync is throttled;
- GitHub is not polled at runtime;
- visual/playlist/skip/previous changes never restart the RTMP session.
"""
import hashlib
import json
import os
import pathlib
import shutil
import time
import subprocess
import signal
import urllib.request
import uuid
import zipfile
import visual_rotation
from datetime import datetime, timezone

STATE=pathlib.Path("/state")
API=os.environ.get("MEDIAFORGE_API_URL","http://127.0.0.1:8790").rstrip("/")
REMOTE_CONTROL_API=os.environ.get("MEDIAFORGE_REMOTE_CONTROL_API","https://mediaforge-api.guilhermeodsgn.workers.dev").rstrip("/")
AGENT_TOKEN=os.environ.get("MEDIAFORGE_AGENT_TOKEN","").strip()
POLL=max(3,int(os.environ.get("OVH_AGENT_POLL_SECONDS","5")))
LOCAL_STATUS_SECONDS=max(5,int(os.environ.get("OVH_LOCAL_STATUS_SECONDS","10")))
REMOTE_STATUS_SECONDS=max(5,int(os.environ.get("OVH_REMOTE_STATUS_SECONDS","10")))
GTA_PLAYLIST_SYNC_SECONDS=max(10,int(os.environ.get("OVH_GTA_PLAYLIST_SYNC_SECONDS","20")))
GITHUB_RAW_BASE=os.environ.get("MEDIAFORGE_GITHUB_RAW_BASE","https://raw.githubusercontent.com/thebusinessflowtv/theofficemusic/main").rstrip("/")
GITHUB_FALLBACK_MAX_AGE_SECONDS=max(30,int(os.environ.get("OVH_GITHUB_FALLBACK_MAX_AGE_SECONDS","900")))
SLOTS=("kick","twitch","youtube-deep-house","youtube-rainy","youtube-gta-vi","youtube-ui-test")
AGENT_DIR=STATE/"agent"
PROCESSED=AGENT_DIR/"processed.json"
LOCAL_STATUS=AGENT_DIR/"status.json"
LOCAL_INBOX=AGENT_DIR/"local-inbox"
AGENT_DIR.mkdir(parents=True,exist_ok=True)
LOCAL_INBOX.mkdir(parents=True,exist_ok=True)
GTA_PROC=None
GTA_LOG=None
UI_TEST_PROC=None
UI_TEST_LOG=None

# These sources represent ordinary hot changes and are never allowed to
# interrupt a live RTMP session, even if an upstream bug labels them restart.
NON_INTERRUPT_SOURCES=(
    "mediaforge-visual-switch",
    "mediaforge-playlist-switch",
    "library-sync",
    "mediaforge-visual-rotation",
)


def iso_now():
    return datetime.now(timezone.utc).isoformat().replace("+00:00","Z")


def read_json(path,default=None):
    try:return json.loads(path.read_text(encoding="utf-8"))
    except Exception:return default


def atomic_json(path,payload):
    path.parent.mkdir(parents=True,exist_ok=True)
    tmp=path.with_suffix(path.suffix+".tmp")
    tmp.write_text(json.dumps(payload,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    tmp.replace(path)


def agent_headers(extra=None):
    headers={"User-Agent":"MediaForge-OVH-Agent"}
    if AGENT_TOKEN:
        headers["x-ovh-agent-token"]=AGENT_TOKEN
    if extra:
        headers.update(extra)
    return headers

def fetch_json(url):
    req=urllib.request.Request(
        url+("&" if "?" in url else "?")+"ts="+str(int(time.time()*1000)),
        headers=agent_headers(),
    )
    with urllib.request.urlopen(req,timeout=20) as r:
        return json.loads(r.read().decode("utf-8"))


def post_json(url,payload):
    data=json.dumps(payload).encode("utf-8")
    req=urllib.request.Request(
        url,
        data=data,
        method="POST",
        headers=agent_headers({"content-type":"application/json"}),
    )
    with urllib.request.urlopen(req,timeout=20) as r:
        return r.read()


def github_fetch_json(path):
    url=GITHUB_RAW_BASE+"/"+str(path).lstrip("/")+"?ts="+str(int(time.time()*1000))
    req=urllib.request.Request(url,headers={"User-Agent":"MediaForge-OVH-Agent-GitHub-Fallback","Cache-Control":"no-cache"})
    with urllib.request.urlopen(req,timeout=20) as r:
        return json.loads(r.read().decode("utf-8"))


def command_is_recent(cmd,entry=None):
    raw=str((cmd or {}).get("requested_at") or (entry or {}).get("created_at") or "")
    if not raw:
        return False
    try:
        dt=datetime.fromisoformat(raw.replace("Z","+00:00"))
        age=(datetime.now(timezone.utc)-dt.astimezone(timezone.utc)).total_seconds()
        return -60 <= age <= GITHUB_FALLBACK_MAX_AGE_SECONDS
    except Exception:
        return False


def slot_for(cmd):
    slot=str(cmd.get("runtime_slot") or cmd.get("slot") or cmd.get("platform") or "")
    if slot=="youtube":
        slot=str(cmd.get("youtube_slot") or "")
    return slot if slot in SLOTS else ""


def next_generation(desired):
    try:return int(desired.get("generation") or 0)+1
    except Exception:return int(time.time())


def next_visual_revision(desired):
    try:return int(desired.get("visual_revision") or 0)+1
    except Exception:return int(time.time())


def source_is_non_interrupting(cmd):
    source=str(cmd.get("source") or "").strip().lower()
    return any(source.startswith(prefix) for prefix in NON_INTERRUPT_SOURCES)


def download_file(url,target):
    target.parent.mkdir(parents=True,exist_ok=True)
    temp=target.with_suffix(target.suffix+".part")
    temp.unlink(missing_ok=True)
    last=None
    for attempt in range(1,4):
        try:
            temp.unlink(missing_ok=True)
            req=urllib.request.Request(
                str(url),
                headers={
                    "User-Agent":"MediaForge-Twitch-DJ-Importer",
                    "Accept":"application/octet-stream,*/*",
                    "Connection":"close",
                },
            )
            # The DJ batch can be hundreds of MB and is streamed from the local
            # MediaForge runtime. Keep a generous socket timeout and retry the
            # full transfer if the local endpoint closes early.
            with urllib.request.urlopen(req,timeout=900) as r, open(temp,"wb") as fh:
                total=0
                while True:
                    chunk=r.read(4*1024*1024)
                    if not chunk:
                        break
                    fh.write(chunk)
                    total+=len(chunk)
                fh.flush()
                os.fsync(fh.fileno())
            if not temp.exists() or temp.stat().st_size<1024:
                raise RuntimeError("DJ archive download is empty")
            temp.replace(target)
            return target
        except Exception as exc:
            last=exc
            size=temp.stat().st_size if temp.exists() else 0
            if attempt>=3:
                raise RuntimeError(f"DJ archive download failed after {size} bytes and {attempt} attempts: {exc}")
            time.sleep(attempt*2)
    raise RuntimeError(f"DJ archive download failed: {last}")

def import_twitch_dj_archive(cmd):
    if slot_for(cmd)!="twitch":
        raise ValueError("DJ archive import is Twitch-only")
    archive_url=str(cmd.get("archive_url") or "").strip()
    manifest=cmd.get("manifest") if isinstance(cmd.get("manifest"),dict) else None
    manifest_url=str(cmd.get("manifest_url") or "").strip()
    if not archive_url:
        raise ValueError("archive_url is required")
    if manifest is None:
        if not manifest_url:
            raise ValueError("manifest or manifest_url is required")
        manifest=fetch_json(manifest_url)
    expected={}
    for row in manifest.get("tracks") or []:
        h=str(row.get("sha256") or "").lower().strip()
        if len(h)==64:
            expected[h]=row
    if not expected:
        raise RuntimeError("DJ manifest has no hashes")

    dj_dir=STATE/"twitch-dj-audio"
    dj_dir.mkdir(parents=True,exist_ok=True)
    archive=STATE/"twitch-dj-import.zip"
    status_path=STATE/"twitch"/"dj-import.json"
    atomic_json(status_path,{"status":"downloading","updated_at":iso_now(),"expected":len(expected)})
    try:
        download_file(archive_url,archive)
    except Exception as exc:
        atomic_json(status_path,{"status":"failed","updated_at":iso_now(),"expected":len(expected),"error":str(exc)[:500],"rtmp_restart":False})
        raise

    found={}
    rejected=[]
    duplicates=0
    atomic_json(status_path,{"status":"validating","updated_at":iso_now(),"expected":len(expected)})
    with zipfile.ZipFile(archive,"r") as zf:
        for info in zf.infolist():
            if info.is_dir() or not str(info.filename).lower().endswith(".mp3"):
                continue
            h=hashlib.sha256()
            with zf.open(info,"r") as src:
                while True:
                    chunk=src.read(1024*1024)
                    if not chunk:
                        break
                    h.update(chunk)
            digest=h.hexdigest()
            meta=expected.get(digest)
            if not meta:
                rejected.append({"filename":pathlib.PurePosixPath(info.filename).name,"sha256":digest})
                continue
            if digest in found:
                duplicates+=1
                continue
            target=dj_dir/(digest+".mp3")
            temp=target.with_suffix(".mp3.part")
            with zf.open(info,"r") as src, open(temp,"wb") as dst:
                shutil.copyfileobj(src,dst,1024*1024)
            temp.replace(target)
            found[digest]={
                "id":"twitch-dj-"+digest[:12],
                "title":str(meta.get("title") or pathlib.PurePosixPath(info.filename).stem),
                "artists":str(meta.get("artists") or ""),
                "url":"file://"+str(target),
                "duration_seconds":float(meta.get("duration_seconds") or 0),
                "source":"twitch_dj_catalog_licensed_copy",
                "sha256":digest,
            }

    missing=sorted(set(expected)-set(found))
    if not found:
        raise RuntimeError("No validated Twitch DJ MP3 found in archive")

    base=[x for x in (cmd.get("base_tracks") or []) if isinstance(x,dict) and x.get("url")]
    mixed=[]
    for i,t in enumerate(base):
        mixed.append({
            "id":str(t.get("id") or f"twitch-dj-original-{i+1:02d}"),
            "title":str(t.get("title") or "Peter Lofi"),
            "url":str(t["url"]),
            "duration_seconds":float(t.get("duration_seconds") or 0),
            "source":"peter_lofi_original",
        })
    mixed.extend(found.values())

    st=STATE/"twitch"
    atomic_json(st/"playlist.json",{
        "station":"twitch",
        "playlist_key":"twitch-dj-mixed",
        "platform_lock":["twitch"],
        "shuffle":True,
        "repeat":True,
        "updated_at":iso_now(),
        "tracks":mixed,
    })
    desired=read_json(st/"desired.json",{}) or {}
    desired.update({
        "runtime":"ovh",
        "runtime_slot":"twitch",
        "playlist_key":"twitch-dj-mixed",
        "updated_at":iso_now(),
    })
    atomic_json(st/"desired.json",desired)
    # Only interrupt the current audio track with a fade; generation remains untouched,
    # so the persistent Twitch RTMP encoder stays connected.
    atomic_json(st/"command.json",{
        "id":str(cmd.get("id") or uuid.uuid4()),
        "action":"skip",
        "requested_at":iso_now(),
        "source":"mediaforge-twitch-dj-import",
    })
    result={
        "status":"ready",
        "updated_at":iso_now(),
        "playlist_key":"twitch-dj-mixed",
        "original_tracks":len(base),
        "commercial_tracks":len(found),
        "track_count":len(mixed),
        "duplicates_ignored":duplicates,
        "missing_hashes":missing,
        "rejected_files":rejected,
        "rtmp_restart":False,
    }
    atomic_json(status_path,result)
    try:
        archive.unlink(missing_ok=True)
    except Exception:
        pass
    return result



def _dj_track_sha_from_row(row):
    digest=str((row or {}).get("sha256") or "").lower().strip()
    if len(digest)==64 and all(ch in "0123456789abcdef" for ch in digest):
        return digest
    url=str((row or {}).get("url") or "")
    name=pathlib.PurePosixPath(url.replace("file://","")).name
    if name.lower().endswith(".mp3"):
        stem=name[:-4].lower()
        if len(stem)==64 and all(ch in "0123456789abcdef" for ch in stem):
            return stem
    return ""


def _probe_dj_mp3(path, fallback_title):
    try:
        raw=subprocess.check_output([
            "ffprobe","-v","error",
            "-show_entries","format=duration:format_tags=title,artist,album_artist",
            "-of","json",str(path)
        ],text=True,stderr=subprocess.STDOUT,timeout=30)
        data=json.loads(raw or "{}")
        fmt=data.get("format") or {}
        duration=float(fmt.get("duration") or 0)
        if duration < 15:
            raise RuntimeError("audio duration too short")
        tags=fmt.get("tags") or {}
        title=str(tags.get("title") or fallback_title or path.stem).strip()
        artists=str(tags.get("artist") or tags.get("album_artist") or "").strip()
        return {"duration_seconds":round(duration,3),"title":title,"artists":artists}
    except Exception as exc:
        raise RuntimeError(f"invalid MP3 ({path.name}): {exc}")


def _queue_audio_skip(slot, source):
    st=STATE/slot
    cid=f"{slot}-dj-{uuid.uuid4().hex}"
    payload={"id":cid,"action":"skip","requested_at":iso_now(),"source":source}
    qdir=st/"audio-commands"
    qdir.mkdir(parents=True,exist_ok=True)
    atomic_json(qdir/f"{time.time_ns():020d}-{cid}.json",payload)
    atomic_json(st/"command.json",payload)


def import_shared_dj_archive(cmd):
    """Import a user-supplied DJ ZIP, dedupe it, then make Twitch+Kick commercial-only.

    Safety order:
    1) download/audit the complete ZIP;
    2) extract all valid new MP3s and stage them into Twitch while originals remain;
    3) validate the staged playlist and require exactly the expected originals;
    4) only then remove originals from Twitch and build an independent Kick copy;
    5) queue audio-only fades/skips; never restart RTMP or containers.
    """
    archive_url=str(cmd.get("archive_url") or "").strip()
    if not archive_url:
        raise ValueError("archive_url is required")

    expected_originals=int(cmd.get("expected_original_tracks") or 36)
    twitch=STATE/"twitch"
    kick=STATE/"kick"
    current=read_json(twitch/"playlist.json",{}) or {}
    current_tracks=[x for x in (current.get("tracks") or []) if isinstance(x,dict) and x.get("url")]
    if not current_tracks:
        raise RuntimeError("current Twitch DJ playlist is empty")

    before_twitch_pid=(read_json(twitch/"health.json",{}) or {}).get("encoder_pid")
    before_kick_pid=(read_json(kick/"health.json",{}) or {}).get("encoder_pid")
    batch_id=str(cmd.get("id") or uuid.uuid4())
    status_path=twitch/"dj-import.json"
    audit_dir=STATE/"dj-batches"
    audit_dir.mkdir(parents=True,exist_ok=True)
    archive=STATE/f"dj-batch-{batch_id}.zip"
    dj_dir=STATE/"twitch-dj-audio"
    kick_dir=STATE/"kick-dj-audio"
    dj_dir.mkdir(parents=True,exist_ok=True)
    kick_dir.mkdir(parents=True,exist_ok=True)

    existing_hashes={}
    existing_commercial=[]
    original_tracks=[]
    for row in current_tracks:
        src=str(row.get("source") or "")
        rid=str(row.get("id") or "")
        is_original=(src=="peter_lofi_original" or rid.startswith("twitch-dj-original-"))
        if is_original:
            original_tracks.append(row)
            continue
        existing_commercial.append(row)
        digest=_dj_track_sha_from_row(row)
        if digest:
            existing_hashes[digest]=row

    atomic_json(status_path,{
        "status":"downloading","updated_at":iso_now(),"batch_id":batch_id,
        "original_tracks":len(original_tracks),
        "commercial_tracks":len(existing_commercial),
        "track_count":len(current_tracks),
        "rtmp_restart":False,
    })
    try:
        download_file(archive_url,archive)
    except Exception as exc:
        atomic_json(status_path,{
            "status":"failed","updated_at":iso_now(),"batch_id":batch_id,
            "error":str(exc)[:500],
            "original_tracks":len(original_tracks),
            "commercial_tracks":len(existing_commercial),
            "track_count":len(current_tracks),
            "rtmp_restart":False,
            "container_restart":False,
        })
        raise

    valid=[]
    rejected=[]
    batch_hashes=set()
    duplicate_in_batch=0
    duplicate_existing=0
    real_mp3s=0
    atomic_json(status_path,{
        "status":"validating","updated_at":iso_now(),"batch_id":batch_id,
        "original_tracks":len(original_tracks),
        "commercial_tracks":len(existing_commercial),
        "track_count":len(current_tracks),
        "rtmp_restart":False,
    })

    with zipfile.ZipFile(archive,"r") as zf:
        infos=[]
        for info in zf.infolist():
            name=str(info.filename)
            base=pathlib.PurePosixPath(name).name
            if info.is_dir() or not name.lower().endswith(".mp3"):
                continue
            if base.startswith("._") or "/__MACOSX/" in ("/"+name):
                continue
            infos.append(info)

        if not infos:
            raise RuntimeError("ZIP has no MP3 files")

        for info in infos:
            real_mp3s+=1
            h=hashlib.sha256()
            with zf.open(info,"r") as src:
                while True:
                    chunk=src.read(1024*1024)
                    if not chunk:
                        break
                    h.update(chunk)
            digest=h.hexdigest()
            filename=pathlib.PurePosixPath(info.filename).name

            if digest in batch_hashes:
                duplicate_in_batch+=1
                continue
            batch_hashes.add(digest)

            if digest in existing_hashes:
                duplicate_existing+=1
                continue

            target=dj_dir/(digest+".mp3")
            created=False
            if not target.exists() or target.stat().st_size<1024:
                temp=target.with_suffix(".mp3.part")
                with zf.open(info,"r") as src, open(temp,"wb") as dst:
                    shutil.copyfileobj(src,dst,1024*1024)
                temp.replace(target)
                created=True
            try:
                stem=pathlib.PurePosixPath(filename).stem
                stem=stem.lstrip("0123456789 ._-") or pathlib.PurePosixPath(filename).stem
                meta=_probe_dj_mp3(target,stem)
                row={
                    "id":"twitch-dj-"+digest[:12],
                    "title":meta["title"],
                    "artists":meta["artists"],
                    "url":"file:///state/twitch-dj-audio/"+target.name,
                    "duration_seconds":meta["duration_seconds"],
                    "source":"twitch_dj_user_batch",
                    "sha256":digest,
                    "import_batch":batch_id,
                    "original_filename":filename,
                }
                valid.append(row)
                existing_hashes[digest]=row
            except Exception as exc:
                if created:
                    target.unlink(missing_ok=True)
                rejected.append({"filename":filename,"sha256":digest,"reason":str(exc)[:300]})

    if not valid and duplicate_existing==0:
        raise RuntimeError("No valid new MP3 found in archive")

    # Stage new commercial tracks while the 36 Peter Lofi originals are still present.
    staged=list(current_tracks)+valid
    atomic_json(twitch/"playlist.json",{
        "station":"twitch",
        "playlist_key":"twitch-dj-mixed-staged",
        "platform_lock":["twitch"],
        "shuffle":True,
        "repeat":True,
        "updated_at":iso_now(),
        "tracks":staged,
    })
    atomic_json(status_path,{
        "status":"staged","updated_at":iso_now(),"batch_id":batch_id,
        "zip_mp3_files":real_mp3s,
        "new_valid_tracks":len(valid),
        "duplicates_existing":duplicate_existing,
        "duplicates_in_batch":duplicate_in_batch,
        "rejected_files":rejected,
        "original_tracks":len(original_tracks),
        "commercial_tracks":len(existing_commercial)+len(valid),
        "track_count":len(staged),
        "rtmp_restart":False,
    })

    # Hard safety rail: originals are removed only after a complete staged playlist exists.
    staged_check=read_json(twitch/"playlist.json",{}) or {}
    staged_tracks=staged_check.get("tracks") or []
    if len(staged_tracks)!=len(staged):
        raise RuntimeError("staged Twitch playlist validation failed")
    staged_originals=[
        t for t in staged_tracks
        if str(t.get("source") or "")=="peter_lofi_original"
        or str(t.get("id") or "").startswith("twitch-dj-original-")
    ]
    if len(staged_originals)!=expected_originals:
        raise RuntimeError(
            f"original-removal safety blocked: expected {expected_originals}, found {len(staged_originals)}"
        )

    final_commercial=[
        t for t in staged_tracks
        if not (
            str(t.get("source") or "")=="peter_lofi_original"
            or str(t.get("id") or "").startswith("twitch-dj-original-")
        )
    ]
    # Deduplicate commercial set by SHA or URL while preserving order.
    unique=[]
    seen=set()
    for row in final_commercial:
        key=_dj_track_sha_from_row(row) or str(row.get("url") or "")
        if not key or key in seen:
            continue
        seen.add(key)
        unique.append(row)
    final_commercial=unique
    if len(final_commercial)<len(existing_commercial):
        raise RuntimeError("commercial-count safety blocked: final set shrank unexpectedly")

    # Back up both live playlists before final replacement.
    backups=STATE/"playlist-backups"
    backups.mkdir(parents=True,exist_ok=True)
    stamp=datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    atomic_json(backups/f"{stamp}-twitch-before-commercial-only.json",current)
    atomic_json(backups/f"{stamp}-kick-before-commercial-only.json",read_json(kick/"playlist.json",{}) or {})

    # Twitch: commercial only.
    final_twitch=[]
    for i,row in enumerate(final_commercial,1):
        t=dict(row);t["position"]=i
        final_twitch.append(t)
    atomic_json(twitch/"playlist.json",{
        "station":"twitch",
        "playlist_key":"twitch-dj-commercial-only",
        "platform_lock":["twitch"],
        "shuffle":True,
        "repeat":True,
        "updated_at":iso_now(),
        "commercial_only":True,
        "tracks":final_twitch,
    })
    td=read_json(twitch/"desired.json",{}) or {}
    td.update({"runtime":"ovh","runtime_slot":"twitch","playlist_key":"twitch-dj-commercial-only","updated_at":iso_now()})
    atomic_json(twitch/"desired.json",td)

    # Kick: independent commercial copy/player. Local files are hard-linked when possible.
    final_kick=[]
    linked=0
    copied=0
    for i,row in enumerate(final_twitch,1):
        t=dict(row)
        t["id"]="kick-copy-"+str(row.get("id") or f"dj-{i:03d}")
        t["position"]=i
        url=str(row.get("url") or "")
        prefix="file:///state/twitch-dj-audio/"
        if url.startswith(prefix):
            filename=url[len(prefix):]
            src=STATE/"twitch-dj-audio"/filename
            dst=kick_dir/filename
            if not src.exists():
                raise RuntimeError(f"commercial audio missing before Kick copy: {src}")
            if not dst.exists():
                try:
                    os.link(src,dst);linked+=1
                except OSError:
                    shutil.copy2(src,dst);copied+=1
            t["url"]="file:///state/kick-dj-audio/"+filename
        t["source"]="kick_independent_commercial_copy"
        final_kick.append(t)

    atomic_json(kick/"playlist.json",{
        "station":"kick",
        "playlist_key":"kick-dj-commercial-only-independent",
        "shuffle":True,
        "repeat":True,
        "updated_at":iso_now(),
        "copied_from":"twitch-dj-commercial-only",
        "commercial_only":True,
        "independent_player":True,
        "tracks":final_kick,
    })
    kd=read_json(kick/"desired.json",{}) or {}
    kd.update({"runtime":"ovh","runtime_slot":"kick","playlist_key":"kick-dj-commercial-only-independent","updated_at":iso_now()})
    atomic_json(kick/"desired.json",kd)

    _queue_audio_skip("twitch","mediaforge-shared-dj-commercial-only")
    _queue_audio_skip("kick","mediaforge-shared-dj-commercial-only")

    after_twitch_pid=(read_json(twitch/"health.json",{}) or {}).get("encoder_pid")
    after_kick_pid=(read_json(kick/"health.json",{}) or {}).get("encoder_pid")
    if before_twitch_pid and after_twitch_pid and before_twitch_pid!=after_twitch_pid:
        raise RuntimeError("Twitch encoder PID changed during audio-only DJ import")
    if before_kick_pid and after_kick_pid and before_kick_pid!=after_kick_pid:
        raise RuntimeError("Kick encoder PID changed during audio-only DJ import")

    result={
        "status":"ready",
        "updated_at":iso_now(),
        "batch_id":batch_id,
        "zip_mp3_files":real_mp3s,
        "new_valid_tracks":len(valid),
        "duplicates_existing":duplicate_existing,
        "duplicates_in_batch":duplicate_in_batch,
        "rejected_files":rejected,
        "original_tracks":0,
        "originals_removed":len(staged_originals),
        "commercial_tracks":len(final_twitch),
        "track_count":len(final_twitch),
        "twitch_track_count":len(final_twitch),
        "kick_track_count":len(final_kick),
        "kick_hardlinks_created":linked,
        "kick_files_copied":copied,
        "rtmp_restart":False,
        "container_restart":False,
        "twitch_encoder_pid":after_twitch_pid or before_twitch_pid,
        "kick_encoder_pid":after_kick_pid or before_kick_pid,
    }
    atomic_json(twitch/"dj-import.json",result)
    atomic_json(kick/"dj-import.json",result)
    atomic_json(audit_dir/f"{batch_id}.json",result)
    archive.unlink(missing_ok=True)
    return result

def add_approved_gaming30_to_dj_mix(cmd):
    """Append the approved batch to both local DJs, keeping their RTMP publishers."""
    from urllib.parse import urlsplit, urlunsplit
    cid=str(cmd.get("id") or "")
    result_path=STATE/"agent"/"gaming30-dj-mix.json"
    previous=read_json(result_path,{}) or {}
    if previous.get("command_id")==cid and previous.get("status")=="ready":
        return previous
    library_path="control/music-library.json"
    library=fetch_json(API+"/api/ovh/agent/runtime-config?path="+library_path+"&raw=1")
    gaming=next((p for p in library.get("playlists",[]) if p.get("key")=="gaming-radio"),{})
    approved=[t for t in gaming.get("tracks",[]) if t.get("source")=="gaming-twitch-dj-30" and t.get("asset_id")]
    expected={f"gaming-twitch-dj-20261004-{n:02d}" for n in range(1,31)}
    if len(approved)!=30 or {t.get("id") for t in approved}!=expected:
        raise RuntimeError("Exactly 30 approved local Gaming tracks are required")
    approved.sort(key=lambda t:t["id"])
    for i,row in enumerate(approved):
        audio_url=urlsplit(str(row.get("url") or ""))
        if float(row.get("duration_seconds") or 0)!=300 or audio_url.scheme not in {"http","https"} or audio_url.netloc!="peterlofi.odsgn.com.br" or not audio_url.path.startswith("/media/"):
            raise RuntimeError("Invalid approved Gaming audio source")
        approved[i]={**row,"url":urlunsplit(audio_url._replace(scheme="https"))}

    before={slot:read_json(STATE/slot/"health.json",{}) or {} for slot in ("twitch","kick")}
    current={slot:read_json(STATE/slot/"playlist.json",{}) or {} for slot in ("twitch","kick")}
    desired={slot:read_json(STATE/slot/"desired.json",{}) or {} for slot in ("twitch","kick")}
    if any(not current[s].get("tracks") for s in current):
        raise RuntimeError("Existing DJ playlists must be preserved")
    if any(before[s].get("status")!="live" for s in before):
        raise RuntimeError("Twitch and Kick must already be live")
    # Validate/cache every new file before changing either live playlist.
    additions={"twitch":[],"kick":[]}
    for row in approved:
        target=STATE/"gaming30-dj-audio"/(row["id"]+".mp3")
        if not target.exists() or target.stat().st_size<3000000:
            download_file(row["url"],target)
        if not 3000000<=target.stat().st_size<=20000000:
            raise RuntimeError("Invalid Gaming MP3 size")
        probe=_probe_dj_mp3(target,row["title"])
        if not 298<=float(probe["duration_seconds"])<=302:
            raise RuntimeError("Gaming audio duration must be five minutes")
        tw={**row,"url":"file:///state/gaming30-dj-audio/"+target.name,"source":"peter_lofi_original","generation_source":"gaming-twitch-dj-30"}
        additions["twitch"].append(tw)
        dest=STATE/"kick-gaming30-dj-audio"/target.name
        dest.parent.mkdir(parents=True,exist_ok=True)
        if not dest.exists():
            try:os.link(target,dest)
            except OSError:shutil.copy2(target,dest)
        additions["kick"].append({**tw,"id":"kick-copy-"+row["id"],"original_track_id":row["id"],"source":"kick_independent_original_copy","url":"file:///state/kick-gaming30-dj-audio/"+target.name})

    keys={"twitch":"twitch-dj-mixed","kick":"kick-dj-mixed-independent"}
    mixed={}
    for slot in ("twitch","kick"):
        new_ids={t["id"] for t in additions[slot]}
        base=[dict(t) for t in current[slot]["tracks"] if t.get("id") not in new_ids]
        tracks=base+additions[slot]
        if len({t["id"] for t in tracks})!=len(tracks):
            raise RuntimeError("Duplicate DJ track IDs")
        for i,t in enumerate(tracks,1):t["position"]=i
        mixed[slot]={**current[slot],"station":slot,"playlist_key":keys[slot],"shuffle":True,"repeat":True,"commercial_only":False,"independent_player":True,"updated_at":iso_now(),"tracks":tracks}
        safe="".join(ch for ch in cid if ch.isalnum() or ch in "-_")[:96]
        backup=STATE/"playlist-backups"/(safe+"-"+slot+"-before-gaming30.json")
        if not backup.exists():atomic_json(backup,{"playlist":current[slot],"desired":desired[slot]})

    # Merge the canonical entries into a fresh copy, preserving every other playlist.
    library=fetch_json(API+"/api/ovh/agent/runtime-config?path="+library_path+"&raw=1")
    for slot in ("twitch","kick"):
        entry=next((p for p in library.get("playlists",[]) if p.get("key")==keys[slot]),None)
        if entry is None:
            entry={"key":keys[slot]};library.setdefault("playlists",[]).append(entry)
        entry.update({"name":"Twitch DJ Mixed" if slot=="twitch" else "Kick DJ Mixed · Independent","tracks":mixed[slot]["tracks"],"track_count":len(mixed[slot]["tracks"]),"total_duration_seconds":sum(float(t.get("duration_seconds") or 0) for t in mixed[slot]["tracks"]),"allowed_platforms":[slot],"shuffle":True,"repeat":True,"commercial_only":False,"independent_player":True})
    library["updated_at"]=iso_now()
    post_json(API+"/api/ovh/agent/runtime-config",{"path":library_path,"payload":library})

    # One audio fade starts a new approved song on each independent player.
    # The complete merged list is restored after the fade; the current decoder
    # keeps playing and neither the video nor the RTMP encoder is restarted.
    started={}
    try:
        for slot in ("twitch","kick"):
            atomic_json(STATE/slot/"playlist.json",{**mixed[slot],"tracks":additions[slot]})
            atomic_json(STATE/slot/"desired.json",{**desired[slot],"playlist_key":keys[slot],"updated_at":iso_now()})
            _queue_audio_skip(slot,"mediaforge-playlist-switch-gaming30")
        end=time.time()+45
        while time.time()<end:
            for slot in ("twitch","kick"):
                n=read_json(STATE/slot/"now-playing.json",{}) or {}
                if n.get("state")=="playing" and n.get("track_id") in {t["id"] for t in additions[slot]}:
                    started[slot]={"track_id":n["track_id"],"title":n.get("title")}
            if len(started)==2:break
            time.sleep(.5)
    finally:
        for slot in ("twitch","kick"):
            atomic_json(STATE/slot/"playlist.json",mixed[slot])
    if len(started)!=2:
        raise RuntimeError("Merged playlists ready, but new-song playback was not confirmed")
    for slot in ("twitch","kick"):
        after=read_json(STATE/slot/"health.json",{}) or {}
        if before[slot].get("encoder_pid")!=after.get("encoder_pid"):
            raise RuntimeError(slot+" RTMP publisher changed")
    result={"status":"ready","command_id":cid,"updated_at":iso_now(),"approved_new_tracks":30,"playlist_counts":{s:len(mixed[s]["tracks"]) for s in mixed},"started":started,"publisher_pids":{s:before[s].get("encoder_pid") for s in before},"rtmp_restart":False,"container_restart":False,"independent_players":True}
    atomic_json(result_path,result)
    return result


def _gta_secret_path():
    return STATE/"youtube-gta-vi"/"runtime-secret.json"


def ensure_gta_process():
    global GTA_PROC, GTA_LOG
    st=STATE/"youtube-gta-vi"
    desired=read_json(st/"desired.json",{}) or {}
    wants_live=str(desired.get("desired") or "stopped").lower() not in {"stopped","stop","offline"}
    if not wants_live:
        if GTA_PROC and GTA_PROC.poll() is None:
            try:
                GTA_PROC.send_signal(signal.SIGTERM)
                GTA_PROC.wait(timeout=8)
            except Exception:
                try: GTA_PROC.kill()
                except Exception: pass
        GTA_PROC=None
        if GTA_LOG:
            try: GTA_LOG.close()
            except Exception: pass
        GTA_LOG=None
        return

    if GTA_PROC and GTA_PROC.poll() is None:
        return

    secret=read_json(_gta_secret_path(),{}) or {}
    stream_url=str(secret.get("stream_url") or "").strip()
    stream_key=str(secret.get("stream_key") or "").strip()
    if not stream_url or not stream_key:
        return

    playlist=st/"playlist.json"
    if not playlist.exists():
        return

    env=os.environ.copy()
    env.update({
        "STREAM_URL":stream_url,
        "STREAM_KEY":stream_key,
        "LOOP_URL":str(desired.get("loop_url") or ""),
        "PLAYLIST_FILE":"/config/youtube-deep-house.json",
        "VIDEO_FPS":"30",
        "VIDEO_BITRATE_KBPS":"4500",
        "VIDEO_BUFSIZE_KBPS":"9000",
        "AUDIO_BITRATE_KBPS":"160",
        "VIDEO_PROFILE":"main",
        "VIDEO_PRESET":"ultrafast",
        "STREAM_PROFILE_VERSION":"gta-vi-stable-1080p30-v3",
        "STARTUP_PREROLL_SECONDS":"1.5",
        "AUDIO_READY_TIMEOUT_SECONDS":"90",
        "VIDEO_UDP_PORT":"19160",
        "BOOTSTRAP_SESSION_ID":str(desired.get("session_id") or "gta-vi"),
        "BOOTSTRAP_TITLE":str(desired.get("title") or "GTA VI - Vice City"),
    })
    GTA_LOG=open(st/"controller.log","ab",buffering=0)
    GTA_PROC=subprocess.Popen(
        ["python","/app/stream_core.py","--platform","youtube-gta-vi"],
        env=env,stdout=GTA_LOG,stderr=GTA_LOG,
    )


def _ui_test_secret_path():
    return STATE/"youtube-ui-test"/"runtime-secret.json"


def ensure_ui_test_process():
    global UI_TEST_PROC, UI_TEST_LOG
    st=STATE/"youtube-ui-test"
    desired=read_json(st/"desired.json",{}) or {}
    wants_live=str(desired.get("desired") or "stopped").lower() not in {"stopped","stop","offline"}
    if not wants_live:
        if UI_TEST_PROC and UI_TEST_PROC.poll() is None:
            try:
                UI_TEST_PROC.send_signal(signal.SIGTERM)
                UI_TEST_PROC.wait(timeout=8)
            except Exception:
                try: UI_TEST_PROC.kill()
                except Exception: pass
        UI_TEST_PROC=None
        if UI_TEST_LOG:
            try: UI_TEST_LOG.close()
            except Exception: pass
            UI_TEST_LOG=None
        return

    if UI_TEST_PROC and UI_TEST_PROC.poll() is None:
        return

    secret=read_json(_ui_test_secret_path(),{}) or {}
    stream_url=str(secret.get("stream_url") or "").strip()
    stream_key=str(secret.get("stream_key") or "").strip()
    if not stream_url or not stream_key:
        return

    playlist=st/"playlist.json"
    if not playlist.exists():
        src=pathlib.Path("/config/youtube-deep-house.json")
        if src.exists():
            shutil.copyfile(src,playlist)

    env=os.environ.copy()
    env.update({
        "STREAM_URL":stream_url,
        "STREAM_KEY":stream_key,
        "LOOP_URL":str(desired.get("loop_url") or ""),
        "PLAYLIST_FILE":"/config/youtube-deep-house.json",
        "VIDEO_FPS":"30",
        "VIDEO_BITRATE_KBPS":"4500",
        "VIDEO_BUFSIZE_KBPS":"9000",
        "AUDIO_BITRATE_KBPS":"160",
        "VIDEO_PROFILE":"main",
        "VIDEO_PRESET":"superfast",
        "STREAM_PROFILE_VERSION":"ui-test-v1",
        "STARTUP_PREROLL_SECONDS":"1.5",
        "AUDIO_READY_TIMEOUT_SECONDS":"90",
        "VIDEO_UDP_PORT":"19140",
        "BOOTSTRAP_SESSION_ID":str(desired.get("session_id") or "ui-test"),
        "BOOTSTRAP_TITLE":str(desired.get("title") or "Peter Lofi UI Test"),
    })
    UI_TEST_LOG=open(st/"controller.log","ab",buffering=0)
    UI_TEST_PROC=subprocess.Popen(
        ["python","/app/stream_core.py","--platform","youtube-ui-test"],
        env=env,stdout=UI_TEST_LOG,stderr=UI_TEST_LOG,
    )


def apply_command(cmd):
    slot=slot_for(cmd)
    if not slot:
        return

    st=STATE/slot
    st.mkdir(parents=True,exist_ok=True)
    desired_path=st/"desired.json"
    desired=read_json(desired_path,{}) or {}
    action=str(cmd.get("action") or "start").lower()

    if slot=="youtube-gta-vi":
        if action in {"start","resume","restart"}:
            stream_url=str(cmd.get("stream_url") or "").strip()
            stream_key=str(cmd.get("stream_key") or "").strip()
            secret_path=_gta_secret_path()
            if stream_url and stream_key:
                atomic_json(secret_path,{"stream_url":stream_url,"stream_key":stream_key,"updated_at":iso_now()})
                try: os.chmod(secret_path,0o600)
                except Exception: pass
            elif not secret_path.exists():
                raise ValueError("stream_url and stream_key are required for first youtube-gta-vi start")

            tracks=cmd.get("tracks")
            if isinstance(tracks,list) and tracks:
                atomic_json(st/"playlist.json",{
                    "station":"youtube-gta-vi",
                    "playlist_key":str(cmd.get("playlist_key") or "gta-vi-vice-city"),
                    "shuffle":bool(cmd.get("shuffle",True)),
                    "repeat":bool(cmd.get("repeat",True)),
                    "updated_at":iso_now(),
                    "tracks":tracks,
                })

            desired.update({
                "runtime":"ovh",
                "runtime_slot":"youtube-gta-vi",
                "session_id":str(cmd.get("session_id") or desired.get("session_id") or "gta-vi"),
                "title":str(cmd.get("title") or desired.get("title") or "GTA VI - Vice City"),
                "loop_url":str(cmd.get("loop_url") or desired.get("loop_url") or ""),
                "playlist_key":str(cmd.get("playlist_key") or desired.get("playlist_key") or "gta-vi-vice-city"),
                "desired":"live",
                "generation":next_generation(desired),
                "visual_revision":next_visual_revision(desired),
                "updated_at":iso_now(),
            })
            atomic_json(desired_path,desired)
            ensure_gta_process()
            return
        if action=="stop":
            desired.update({
                "desired":"stopped",
                "generation":next_generation(desired),
                "updated_at":iso_now(),
            })
            atomic_json(desired_path,desired)
            ensure_gta_process()
            return

    if slot=="youtube-ui-test":
        if action in {"start","resume","restart"}:
            stream_url=str(cmd.get("stream_url") or "").strip()
            stream_key=str(cmd.get("stream_key") or "").strip()
            secret_path=_ui_test_secret_path()
            if stream_url and stream_key:
                atomic_json(secret_path,{"stream_url":stream_url,"stream_key":stream_key,"updated_at":iso_now()})
                try: os.chmod(secret_path,0o600)
                except Exception: pass
            elif not secret_path.exists():
                raise ValueError("stream_url and stream_key are required for first youtube-ui-test start")

            assets_dir=STATE/"ui-test-assets"
            assets_dir.mkdir(parents=True,exist_ok=True)
            header_url=str(cmd.get("header_url") or "").strip()
            web_url=str(cmd.get("web_url") or "").strip()
            icon_url=str(cmd.get("icon_url") or "").strip()
            font_carrier_url=str(cmd.get("font_carrier_url") or "").strip()
            header_path=assets_dir/"latest-subscriptions.png"
            web_path=assets_dir/"spider-web.png"
            icon_path=assets_dir/"subscriber-icon.png"
            font_path=assets_dir/"superstar.ttf"
            if header_url and not header_path.exists():
                download_file(header_url,header_path)
            if web_url and not web_path.exists():
                download_file(web_url,web_path)
            if icon_url and not icon_path.exists():
                download_file(icon_url,icon_path)
            if font_carrier_url and not font_path.exists():
                carrier=assets_dir/"font-carrier.png"
                download_file(font_carrier_url,carrier)
                raw=subprocess.check_output([
                    "ffmpeg","-v","error","-i",str(carrier),
                    "-f","rawvideo","-pix_fmt","rgb24","pipe:1"
                ])
                if len(raw)<8:
                    raise RuntimeError("font carrier decode failed")
                size=int.from_bytes(raw[:4],"big")
                data=raw[4:4+size]
                if len(data)!=size or size<1000:
                    raise RuntimeError("font carrier payload invalid")
                font_path.write_bytes(data)
                carrier.unlink(missing_ok=True)
            if not header_path.exists() or not web_path.exists() or not icon_path.exists() or not font_path.exists():
                raise RuntimeError("ui-test Figma/font assets are missing")

            if not (st/"playlist.json").exists():
                src=pathlib.Path("/config/youtube-deep-house.json")
                if src.exists():
                    shutil.copyfile(src,st/"playlist.json")

            desired.update({
                "runtime":"ovh",
                "runtime_slot":"youtube-ui-test",
                "session_id":str(cmd.get("session_id") or desired.get("session_id") or "ui-test"),
                "title":str(cmd.get("title") or desired.get("title") or "Peter Lofi UI Test"),
                "loop_url":str(cmd.get("loop_url") or desired.get("loop_url") or ""),
                "playlist_key":"deep-house-radio-test",
                "desired":"live",
                "generation":next_generation(desired),
                "visual_revision":next_visual_revision(desired),
                "updated_at":iso_now(),
            })
            atomic_json(desired_path,desired)
            ensure_ui_test_process()
            return
        if action=="stop":
            desired.update({
                "desired":"stopped",
                "generation":next_generation(desired),
                "updated_at":iso_now(),
            })
            atomic_json(desired_path,desired)
            ensure_ui_test_process()
            return

    if action=="import_twitch_dj_archive":
        import_twitch_dj_archive(cmd)
        return
    if action=="import_shared_dj_archive":
        import_shared_dj_archive(cmd)
        return
    if action=="add_approved_gaming30_to_dj_mix":
        try:
            add_approved_gaming30_to_dj_mix(cmd)
        except Exception as exc:
            atomic_json(AGENT_DIR/"gaming30-dj-mix.json",{"status":"failed","command_id":str(cmd.get("id") or ""),"updated_at":iso_now(),"error":str(exc)[:500]})
            raise
        return

    # Playlist payloads are persisted locally on OVH. AudioEngine keeps a
    # persistent local cache under /state/audio-cache.
    tracks=cmd.get("tracks")
    if isinstance(tracks,list) and tracks:
        atomic_json(st/"playlist.json",{
            "station":slot,
            "playlist_key":str(cmd.get("playlist_key") or ""),
            "shuffle":bool(cmd.get("shuffle",True)),
            "repeat":bool(cmd.get("repeat",True)),
            "updated_at":iso_now(),
            "tracks":tracks,
        })

    if action in {"skip","previous"}:
        # Realtime audio controls are queued as unique files so rapid clicks can
        # never overwrite each other. command.json is kept only as a legacy
        # mirror for older AudioEngine builds; the new engine de-duplicates IDs.
        command_id=str(cmd.get("id") or uuid.uuid4())
        payload={
            "id":command_id,
            "action":action,
            "requested_at":iso_now(),
            "source":str(cmd.get("source") or "mediaforge"),
        }
        qdir=st/"audio-commands"
        qdir.mkdir(parents=True,exist_ok=True)
        safe_id="".join(ch for ch in command_id if ch.isalnum() or ch in "-_")[:96] or uuid.uuid4().hex
        qname=f"{time.time_ns():020d}-{safe_id}.json"
        atomic_json(qdir/qname,payload)
        atomic_json(st/"command.json",payload)
        return

    if action in {"update_playlist","set_playlist"}:
        desired=read_json(desired_path,{}) or {}
        desired.update({
            "runtime":"ovh",
            "runtime_slot":slot,
            "session_id":str(cmd.get("session_id") or desired.get("session_id") or ""),
            "title":str(cmd.get("title") or desired.get("title") or ""),
            "playlist_key":str(cmd.get("playlist_key") or desired.get("playlist_key") or ""),
            "updated_at":iso_now(),
        })
        atomic_json(desired_path,desired)
        if action=="set_playlist":
            atomic_json(st/"command.json",{
                "id":str(cmd.get("id") or uuid.uuid4()),
                "action":"skip",
                "requested_at":iso_now(),
                "source":"mediaforge-playlist-switch",
            })
        return

    if action=="set_visual":
        loop_url=str(cmd.get("loop_url") or "").strip()
        if not loop_url:
            raise ValueError("loop_url is required for set_visual")
        desired=read_json(desired_path,{}) or {}
        desired.update({
            "runtime":"ovh",
            "runtime_slot":slot,
            "session_id":str(cmd.get("session_id") or desired.get("session_id") or ""),
            "title":str(cmd.get("title") or desired.get("title") or ""),
            "loop_url":loop_url,
            "desired":"live",
            # Hot visual change: generation is intentionally untouched.
            "visual_revision":next_visual_revision(desired),
            "updated_at":iso_now(),
        })
        atomic_json(desired_path,desired)
        return

    desired.update({
        "runtime":"ovh",
        "runtime_slot":slot,
        "session_id":str(cmd.get("session_id") or desired.get("session_id") or ""),
        "title":str(cmd.get("title") or desired.get("title") or ""),
        "loop_url":str(cmd.get("loop_url") or desired.get("loop_url") or ""),
        "updated_at":iso_now(),
    })

    # Safety rail: ordinary visual/library operations are forbidden from
    # becoming a restart/stop, even if the UI accidentally labels them so.
    if action in {"restart","stop"} and source_is_non_interrupting(cmd):
        raise ValueError(f"{action} blocked for non-interrupting source {cmd.get('source')}")

    if action=="stop":
        desired["desired"]="stopped"
        desired["generation"]=next_generation(desired)
    elif action=="restart":
        desired["desired"]="live"
        desired["generation"]=next_generation(desired)
    elif action in {"start","resume"}:
        # Idempotent when already live: do not bump generation and therefore do
        # not tear down the current RTMP connection.
        was_live=str(desired.get("desired") or "live").lower() not in {"stopped","stop","offline"}
        desired["desired"]="live"
        if not was_live:
            desired["generation"]=next_generation(desired)

    atomic_json(desired_path,desired)


def host_metrics():
    out={}
    try:
        out["load_1m"]=float(pathlib.Path("/proc/loadavg").read_text().split()[0])
    except Exception:pass
    try:
        vals={}
        for line in pathlib.Path("/proc/meminfo").read_text().splitlines():
            if ":" in line:
                k,v=line.split(":",1)
                vals[k]=int(v.strip().split()[0])
        total=vals.get("MemTotal",0)
        avail=vals.get("MemAvailable",0)
        if total:
            out["memory_percent"]=round((total-avail)*100/total,1)
    except Exception:pass
    try:
        du=shutil.disk_usage("/state")
        out["disk_percent"]=round((du.total-du.free)*100/du.total,1)
    except Exception:pass
    try:
        out["uptime_seconds"]=float(pathlib.Path("/proc/uptime").read_text().split()[0])
    except Exception:pass
    return out


def service_payload(slot):
    st=STATE/slot
    h=read_json(st/"health.json",{}) or {}
    n=read_json(st/"now-playing.json",{}) or {}
    d=read_json(st/"desired.json",{}) or {}
    ah=read_json(st/"audio-health.json",{}) or {}
    vh=read_json(st/"visual-health.json",{}) or {}
    dj=read_json(st/"dj-import.json",{}) or {}
    playlist=read_json(st/"playlist.json",{}) or {}
    return {
        "runtime_slot":slot,
        "platform":h.get("platform") or slot,
        "session_id":h.get("session_id") or d.get("session_id") or "",
        "title":h.get("title") or d.get("title") or "",
        "playlist_key":d.get("playlist_key") or "",
        "status":h.get("status") or ("live" if d.get("desired")=="live" else "unknown"),
        "fps":h.get("fps"),
        "video_bitrate_kbps":h.get("video_bitrate_kbps"),
        "restarts":h.get("restarts",0),
        "updated_at":h.get("updated_at"),
        "loop_url":h.get("loop_url") or d.get("loop_url") or "",
        "visual_revision":d.get("visual_revision") or 0,
        "hot_swap":bool(h.get("hot_swap",False)),
        "encoder_pid":h.get("encoder_pid"),
        "audio_pid":h.get("audio_pid"),
        "visual_pid":h.get("visual_pid"),
        "audio_status":ah.get("state") or ah.get("status"),
        "queued_actions":int(ah.get("queued_actions") or 0),
        "audio_stalls":ah.get("stalls",0),
        "visual_status":vh.get("status"),
        "now_playing":n,
        "playlist_track_count":len(playlist.get("tracks") or []),
        "dj_import":dj,
    }



def _gta_playlist_from_library(library):
    if not isinstance(library,dict):
        return {}
    return next(
        (p for p in (library.get("playlists") or []) if isinstance(p,dict) and p.get("key")=="gta-vi-vice-city"),
        {},
    )


def _best_gta_catalog():
    """Prefer OVH-local catalog; use GitHub only when it has a newer/larger GTA playlist.

    Returns source, chosen GTA playlist, local library and remote library so the
    local MediaForge selector can be repaired without overwriting unrelated
    local-only playlists.
    """
    local={}
    remote={}
    candidates=[]
    try:
        local=fetch_json(API+"/api/ovh/agent/runtime-config?path=control/music-library.json&raw=1") or {}
        gta=_gta_playlist_from_library(local)
        if gta:
            candidates.append(("ovh-local",local,gta))
    except Exception as exc:
        print("GTA catalog local fetch failed:",exc,flush=True)

    try:
        remote=github_fetch_json("control/music-library.json") or {}
        gta=_gta_playlist_from_library(remote)
        if gta:
            candidates.append(("github-fallback",remote,gta))
    except Exception as exc:
        print("GTA catalog GitHub fallback failed:",exc,flush=True)

    if not candidates:
        return "",{},local,remote
    def score(row):
        gta=row[2]
        tracks=[t for t in (gta.get("tracks") or []) if isinstance(t,dict) and t.get("url")]
        return (len(tracks), str((row[1] or {}).get("updated_at") or ""))
    source,library,gta=max(candidates,key=score)
    return source,gta,local,remote


def _sync_gta_into_local_music_library(local,remote):
    """Mirror only the GTA playlist into OVH local_config, preserving all other local playlists."""
    remote_gta=_gta_playlist_from_library(remote)
    if not remote_gta:
        return False
    local=local if isinstance(local,dict) else {}
    current=_gta_playlist_from_library(local)
    old_tracks=[t for t in (current.get("tracks") or []) if isinstance(t,dict) and t.get("url")] if current else []
    new_tracks=[t for t in (remote_gta.get("tracks") or []) if isinstance(t,dict) and t.get("url")]
    if current and len(old_tracks)>=len(new_tracks):
        return False

    playlists=[p for p in (local.get("playlists") or []) if isinstance(p,dict) and p.get("key")!="gta-vi-vice-city"]
    merged={
        **local,
        "version":local.get("version") or remote.get("version") or 1,
        "updated_at":remote.get("updated_at") or iso_now(),
        "playlists":[*playlists,remote_gta],
    }
    post_json(API+"/api/ovh/agent/runtime-config",{
        "path":"control/music-library.json",
        "payload":merged,
    })
    print("GTA playlist mirrored into OVH local music library:",len(new_tracks),flush=True)
    return True


def sync_gta_runtime_playlists():
    """Hot-sync GTA VI catalog into any active runtime using gta-vi-vice-city.

    This only replaces /state/<slot>/playlist.json atomically. AudioEngine
    notices the mtime change, keeps the current decoder/RTMP publisher alive,
    and uses the expanded list for subsequent track choices.
    """
    source,gta,local_library,remote_library=_best_gta_catalog()
    if not gta:
        return {"status":"no-catalog","updated_slots":[]}

    local_library_synced=False
    if remote_library:
        try:
            local_library_synced=_sync_gta_into_local_music_library(local_library,remote_library)
        except Exception as exc:
            print("GTA local music-library mirror failed:",exc,flush=True)

    canonical=[]
    seen=set()
    for row in gta.get("tracks") or []:
        if not isinstance(row,dict):
            continue
        tid=str(row.get("id") or "").strip()
        url=str(row.get("url") or "").strip()
        if not tid or not url or tid in seen:
            continue
        try:
            duration=float(row.get("duration_seconds") or 0)
        except Exception:
            duration=0
        if duration and not 295 <= duration <= 305:
            continue
        seen.add(tid)
        canonical.append({
            "id":tid,
            "title":str(row.get("title") or tid),
            "url":url,
            "source":str(row.get("source") or "gta-vi-vice-city"),
            "artists":str(row.get("artists") or ""),
            "duration_seconds":duration or 300,
            "position":row.get("position"),
            "target_bpm":row.get("target_bpm"),
        })

    if not canonical:
        return {"status":"empty-catalog","source":source,"updated_slots":[]}

    updated=[]
    for slot in SLOTS:
        st=STATE/slot
        desired=read_json(st/"desired.json",{}) or {}
        current=read_json(st/"playlist.json",{}) or {}
        desired_key=str(desired.get("playlist_key") or "")
        current_key=str(current.get("playlist_key") or "")
        if "gta-vi-vice-city" not in {desired_key,current_key}:
            continue

        old_tracks=current.get("tracks") or []
        old_ids=[str(t.get("id") or "") for t in old_tracks if isinstance(t,dict)]
        new_ids=[t["id"] for t in canonical]
        if old_ids==new_ids:
            continue

        payload={
            **current,
            "station":slot,
            "playlist_key":"gta-vi-vice-city",
            "name":"GTA VI - Vice City",
            "shuffle":True,
            "repeat":True,
            "updated_at":iso_now(),
            "catalog_source":source,
            "auto_sync":True,
            "tracks":canonical,
        }
        atomic_json(st/"playlist.json",payload)
        updated.append({
            "slot":slot,
            "before":len(old_ids),
            "after":len(new_ids),
        })

    result={
        "status":"synced" if updated else "current",
        "source":source,
        "catalog_tracks":len(canonical),
        "updated_slots":updated,
        "checked_at":iso_now(),
        "rtmp_restart":False,
        "force_skip":False,
        "local_music_library_synced":local_library_synced,
    }
    atomic_json(AGENT_DIR/"gta-playlist-sync.json",result)
    if updated:
        print("GTA runtime playlist hot-sync:",result,flush=True)
    return result



def status_payload(processed_count=0,last_command=None):
    payload={
        "agent_id":"ovh-main",
        "runtime":"ovh",
        "authority":"local",
        "cloudflare_required_for_live":False,
        "github_runtime_polling":"catalog-fallback-only",
        "gta_playlist_auto_sync":read_json(AGENT_DIR/"gta-playlist-sync.json",{}),
        "gaming_dj30_mix_supported":True,
        "gaming_dj30_mix_version":2,
        "gaming_dj30_mix":read_json(AGENT_DIR/"gaming30-dj-mix.json",{}),
        "visual_rotation":read_json(AGENT_DIR/"visual-rotation-status.json",{}),
        "reported_at":iso_now(),
        "host":host_metrics(),
        "services":{s:service_payload(s) for s in SLOTS},
        "processed_commands":processed_count,
    }
    if last_command:
        payload["last_command"]=last_command
    return payload


def write_local_status(processed_count=0,last_command=None):
    payload=status_payload(processed_count,last_command)
    atomic_json(LOCAL_STATUS,payload)
    return payload


def report_remote(payload):
    # Cloudflare is a UI/control-plane mirror only. If unavailable or capped,
    # local streams and local status continue untouched.
    try:
        post_json(API+"/api/ovh/agent/status",payload)
        return True
    except Exception as exc:
        print("remote status sync failed:",exc,flush=True)
        return False


def process_one_command(cmd,processed,transport,ack_api=None):
    cid=str((cmd or {}).get("id") or "")
    ack_base=(ack_api or API).rstrip("/")
    if not cid:
        return None
    try:
        if cid not in processed:
            apply_command(cmd)
            processed.add(cid)
            atomic_json(PROCESSED,sorted(processed)[-500:])
        last_cmd={
            "id":cid,
            "action":cmd.get("action"),
            "runtime_slot":slot_for(cmd),
            "processed_at":iso_now(),
            "transport":transport,
        }
        try:
            post_json(ack_base+"/api/ovh/agent/command-ack",{"id":cid,"status":"completed"})
        except Exception as ack_exc:
            print("command ack failed:",cid,ack_exc,flush=True)
        print("processed",last_cmd,flush=True)
        return last_cmd
    except Exception as exc:
        try:
            post_json(ack_base+"/api/ovh/agent/command-ack",{
                "id":cid,
                "status":"failed",
                "error":str(exc)[:500],
            })
        except Exception:
            pass
        print("command apply failed",cid,exc,flush=True)
        return None


def poll_local_inbox(processed):
    handled=[]
    try:
        LOCAL_INBOX.mkdir(parents=True,exist_ok=True)
        for path in sorted(LOCAL_INBOX.glob("*.json")):
            try:
                cmd=read_json(path,{}) or {}
                cid=str(cmd.get("id") or "")
                if not cid:
                    path.unlink(missing_ok=True)
                    continue
                if cid in processed:
                    path.unlink(missing_ok=True)
                    continue
                last=process_one_command(cmd,processed,"local-ovh-inbox")
                if last:
                    handled.append(last)
                    path.unlink(missing_ok=True)
            except Exception as exc:
                print("local inbox command failed:",path.name,exc,flush=True)
    except Exception as exc:
        print("local inbox poll failed:",exc,flush=True)
    return handled


def poll_github_fallback(processed):
    handled=[]
    try:
        idx=github_fetch_json("control/ovh-commands/index.json") or {}
        entries=list(idx.get("commands") or [])[-50:]
        for entry in entries:
            cid=str((entry or {}).get("id") or "")
            path=str((entry or {}).get("path") or "")
            if not cid or not path or cid in processed:
                continue
            try:
                cmd=github_fetch_json(path)
            except Exception as exc:
                print("github fallback command fetch failed:",cid,exc,flush=True)
                continue
            if not isinstance(cmd,dict) or str(cmd.get("id") or "")!=cid:
                continue
            if not command_is_recent(cmd,entry):
                continue
            last=process_one_command(cmd,processed,"github-fallback")
            if last:
                handled.append(last)
    except Exception as exc:
        print("github fallback poll failed:",exc,flush=True)
    return handled


def main():
    processed=set(read_json(PROCESSED,[]) or [])
    last_local=0.0
    last_remote=0.0
    last_cmd=None
    last_gta_sync=0.0

    while True:
        try:
            visual_rotation.tick(STATE,read_json,atomic_json,apply_command)
        except Exception as exc:
            print("visual rotation failed:",exc,flush=True)

        try:
            ensure_gta_process()
        except Exception as exc:
            print("GTA YouTube supervisor failed:",exc,flush=True)

        try:
            ensure_ui_test_process()
        except Exception as exc:
            print("ui test supervisor failed:",exc,flush=True)

        local_handled=poll_local_inbox(processed)
        if local_handled:
            last_cmd=local_handled[-1]

        cloud_ok=False
        # Cloudflare is the primary inbox, but GitHub is an independent fallback
        # because every MediaForge command is already mirrored there.
        try:
            batch=fetch_json(API+"/api/ovh/agent/commands?limit=20")
            cloud_ok=True
            for cmd in batch.get("commands") or []:
                last=process_one_command(cmd,processed,"cloudflare-control")
                if last:
                    last_cmd=last
        except Exception as exc:
            print("cloud control poll failed:",exc,flush=True)

        # The public GitHub-Pages MediaForge currently posts realtime controls
        # to the remote API. Consume that queue as a secondary inbox while the
        # local OVH API remains authoritative for runtime/status. This does not
        # make live playback depend on Cloudflare: if it is unavailable, the
        # local inbox and live continue normally.
        if REMOTE_CONTROL_API and REMOTE_CONTROL_API != API:
            try:
                remote=fetch_json(REMOTE_CONTROL_API+"/api/ovh/agent/commands?limit=20")
                for cmd in remote.get("commands") or []:
                    last=process_one_command(cmd,processed,"remote-ui-control",ack_api=REMOTE_CONTROL_API)
                    if last:
                        last_cmd=last
            except Exception as exc:
                print("remote UI control poll failed:",exc,flush=True)

        # GitHub remains a fallback only for non-realtime legacy commands.
        fallback_handled=poll_github_fallback(processed)
        if fallback_handled:
            last_cmd=fallback_handled[-1]

        ts=time.time()
        if ts-last_gta_sync>=GTA_PLAYLIST_SYNC_SECONDS:
            try:
                sync_gta_runtime_playlists()
            except Exception as exc:
                print("GTA runtime playlist sync failed:",exc,flush=True)
            last_gta_sync=ts

        payload=None
        if ts-last_local>=LOCAL_STATUS_SECONDS:
            payload=write_local_status(len(processed),last_cmd)
            last_local=ts
        if ts-last_remote>=REMOTE_STATUS_SECONDS:
            if payload is None:
                payload=write_local_status(len(processed),last_cmd)
                last_local=ts
            report_remote(payload)
            last_remote=ts

        time.sleep(POLL)


if __name__=="__main__":
    main()
