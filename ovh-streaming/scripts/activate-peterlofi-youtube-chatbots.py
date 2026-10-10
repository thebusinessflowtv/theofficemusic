#!/usr/bin/env python3
"""Activate two isolated PeterLofi YouTube chat bots only after authenticated preflight.

Requires Google YouTube OAuth credentials with 'youtube' scope. Credentials are
typed directly into the OVH terminal, hidden, chmod 0600, never printed.
Does NOT recreate or restart any YouTube/Kick/Twitch live publisher containers.
"""
from __future__ import annotations
import getpass
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT=Path("/home/ubuntu/theofficemusic/ovh-streaming")
COMPOSE=ROOT/"docker-compose.yml"
SECRETS=Path("/etc/peterlofi-youtube-chatbot.env")
SERVICES=("youtube-hip-hop-chatbot","youtube-gta-vi-chatbot")
REQUIRED=("YOUTUBE_CLIENT_ID","YOUTUBE_CLIENT_SECRET","YOUTUBE_REFRESH_TOKEN")


def run(command,*,timeout=600):
    print("Running:", " ".join(command),flush=True)
    env={**os.environ,"COMPOSE_PROJECT_NAME":"ovh-streaming"}
    subprocess.run(command,check=True,timeout=timeout,cwd=ROOT,env=env)


def store(data):
    lines=["CHAT_STATE_ROOT=/state"]
    for key in REQUIRED:
        val=data[key]
        if not val or len(val)>5000 or "\n" in val or "\r" in val or "=" in val[:1]:
            raise RuntimeError("invalid "+key)
        # Docker env-file accepts unquoted strings for these OAuth values.
        lines.append(key+"="+val)
    handle,tmp=tempfile.mkstemp(prefix=".youtube-bot-",dir="/etc")
    try:
        os.fchmod(handle,0o600)
        with os.fdopen(handle,"w") as stream:
            stream.write("\n".join(lines)+"\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp,SECRETS)
    finally:
        if os.path.exists(tmp):os.unlink(tmp)


def configure():
    if SECRETS.exists():
        answer=input("Use existing private YouTube credentials? [Y/n]: ").strip().lower()
        if answer in ("","y","yes"):return
    print("\nEnter the SAME Google OAuth credentials previously authorized for Peter Lofi.")
    print("Required OAuth scope: https://www.googleapis.com/auth/youtube")
    client=getpass.getpass("Google Client ID (hidden): ").strip()
    secret=getpass.getpass("Google Client Secret (hidden): ").strip()
    refresh=getpass.getpass("Google OAuth Refresh Token (hidden): ").strip()
    if not re.fullmatch(r"[A-Za-z0-9_.-]{15,250}",client):
        raise RuntimeError("Invalid Google Client ID")
    if len(secret)<6 or len(refresh)<20:
        raise RuntimeError("Missing Google client secret or refresh token")
    if SECRETS.exists():
        print("Existing secrets will be updated privately.")
    store(dict(zip(REQUIRED,(client,secret,refresh))))
    print("Credential file stored with 0600 permissions. Nothing printed.",flush=True)


def main():
    if os.geteuid()!=0:raise RuntimeError("Run with sudo ON THE OVH VPS, not the Mac")
    if not COMPOSE.exists():raise RuntimeError("OVH streaming folder missing")
    if not (ROOT/"app/youtube_chatbot.py").exists():
        raise RuntimeError("Run git pull --ff-only in theofficemusic first")
    option=sys.argv[1] if len(sys.argv)>1 else "--activate"
    if option=="--status":
        for service in SERVICES:
            run(["docker","compose","-f",str(COMPOSE),"ps",service],timeout=45)
        return
    if option not in ("--activate","--preflight"):
        raise RuntimeError("Usage: --activate | --preflight | --status")
    configure()
    run(["docker","compose","-f",str(COMPOSE),"build",*SERVICES],timeout=800)
    for service in SERVICES:
        print("Preflight",service,"— read-only Google channel and live-chat validation.",flush=True)
        run(["docker","compose","-f",str(COMPOSE),"run","--rm","--no-deps",service,
            "python","-u","/app/youtube_chatbot.py","--preflight"],timeout=130)
    if option=="--preflight":
        print("BOTH_YOUTUBE_CHAT_PREFLIGHTS_PASSED — bots not started.")
        return
    print("Both official YouTube broadcasts and credentials verified.",flush=True)
    print("Creating ONLY the two YouTube chat bots. RTMP publishers left untouched.",flush=True)
    run(["docker","compose","-f",str(COMPOSE),"up","-d","--no-deps",
         "--force-recreate",*SERVICES],timeout=150)
    print("YOUTUBE_CHAT_BOTS_STARTED; read runtime status/logs and test !song in both chats.")
    print("No Twitch, Kick or YouTube streaming publisher was intentionally restarted.")


if __name__=="__main__":
    try:main()
    except KeyboardInterrupt:raise SystemExit("Cancelled. No publisher was restarted.")
    except (RuntimeError,subprocess.SubprocessError,OSError) as err:
        print("YOUTUBE_CHAT_SETUP_BLOCKED:",str(err)[:350],file=sys.stderr)
        raise SystemExit(1)
