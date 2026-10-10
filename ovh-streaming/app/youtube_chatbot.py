"""Isolated YouTube Live Chat bot for the Peter Lofi Hip Hop/GTA radio slots.

Only authenticated Google YouTube Data API v3 and official streamList gRPC are used.
Never touch live encoders, RTMP keys, Twitch or Kick. All commands are per-slot.
"""
from __future__ import annotations
import json
import os
import re
import sys
import threading
import time
from pathlib import Path

import grpc
import requests
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
import youtube_stream_list_pb2 as pb2
import youtube_stream_list_pb2_grpc as stub_module

from bot_conversation import plan_chat_reply
from chat_requests import COMMANDS, _atomic_json, _read_json, english_chat_reply, process_chat_message

SLOTS={
    "youtube-lofi-hip-hop":"W89tRiluh_Y",
    "youtube-gta-vi":"5O4ad_F5lGI",
}
CHANNEL_ID="UCxO4Cl7xCdcTsWkmxD_6Wfg"
STATE=Path(os.getenv("CHAT_STATE_ROOT","/state"))
SLOT=os.getenv("YOUTUBE_BOT_SLOT","").strip()
VIDEO_ID=os.getenv("YOUTUBE_VIDEO_ID","").strip()
ROOT=STATE/SLOT
PERIOD_SECONDS=5400
MIN_SEND_SECONDS=8
INTERACTION_MESSAGES=(
    "Hey chat! 🎧 Are you studying, gaming, or relaxing today?",
    "Welcome to PeterLofi Radio! 📚 Type !song to find out what's playing.",
    "Thanks for tuning in! 🎮 Where are you listening from?",
    "What's your favorite game to play with lofi music in the background? 🎵",
    "Love the music? Tell us what you're working on today! 💻",
)
_send_lock=threading.Lock()
_oauth_lock=threading.Lock()
_last_send=0.0
_chat_id=""
_credentials=None


def env_credentials():
    for key in ("YOUTUBE_CLIENT_ID","YOUTUBE_CLIENT_SECRET","YOUTUBE_REFRESH_TOKEN"):
        if not os.environ.get(key):raise RuntimeError("missing "+key)
    return Credentials(
        token=None,
        refresh_token=os.environ["YOUTUBE_REFRESH_TOKEN"],
        token_uri="https://oauth2.googleapis.com/token",
        client_id=os.environ["YOUTUBE_CLIENT_ID"],
        client_secret=os.environ["YOUTUBE_CLIENT_SECRET"],
        scopes=["https://www.googleapis.com/auth/youtube"],
    )


def token():
    global _credentials
    if _credentials is None:
        _credentials=env_credentials()
    with _oauth_lock:
        if not _credentials.valid or _credentials.expired:
            _credentials.refresh(Request())
        if not _credentials.token:raise RuntimeError("youtube_oauth_no_access_token")
        return _credentials.token


def youtube(method,path,params=None,payload=None):
    url="https://www.googleapis.com/youtube/v3/"+path
    headers={"authorization":"Bearer "+token(),"content-type":"application/json"}
    try:
        r=requests.request(method,url,params=params,json=payload,headers=headers,timeout=22)
        if r.status_code==401:
            with _oauth_lock:
                _credentials.refresh(Request())
                headers["authorization"]="Bearer "+_credentials.token
            r=requests.request(method,url,params=params,json=payload,headers=headers,timeout=22)
        if not r.ok:raise RuntimeError("youtube_http_"+str(r.status_code)+"_"+path.replace("/","_"))
        return r.json()
    except requests.exceptions.RequestException as exc:
        raise RuntimeError("youtube_network_error") from exc


def confirmed_chat():
    """Fail closed when broadcast isn't currently live on the authorized channel."""
    ch=youtube("GET","channels",{"part":"id","mine":"true"})
    channel=(ch.get("items") or [{}])[0].get("id")
    if channel!=CHANNEL_ID:raise RuntimeError("youtube_wrong_channel")
    b=youtube("GET","liveBroadcasts",{"part":"id,status,snippet","id":VIDEO_ID})
    items=b.get("items") or []
    if len(items)!=1:raise RuntimeError("youtube_broadcast_not_found")
    live=items[0]
    if (live.get("status") or {}).get("lifeCycleStatus")!="live":
        raise RuntimeError("youtube_broadcast_not_live")
    chat=str((live.get("snippet") or {}).get("liveChatId") or "")
    if not chat or len(chat)>256:raise RuntimeError("youtube_live_chat_unavailable")
    return chat


def runtime(status,**details):
    ROOT.mkdir(parents=True,exist_ok=True)
    _atomic_json(ROOT/"youtube-chat-bot-runtime.json",{
        "service":"peterlofi-youtube-chatbot","slot":SLOT,"video_id":VIDEO_ID,
        "status":status,"updated_at":time.time(),
        "interaction_period_seconds":PERIOD_SECONDS,"commands_enabled":True,**details})


def send(message):
    global _last_send
    message=" ".join(str(message or "").split())[:200]
    if not message or not _chat_id:return False
    # Serialize automatic announcements with viewer-command replies.
    with _send_lock:
        delta=MIN_SEND_SECONDS-(time.monotonic()-_last_send)
        if delta>0:time.sleep(delta)
        payload={"snippet":{"liveChatId":_chat_id,"type":"textMessageEvent",
                            "textMessageDetails":{"messageText":message}}}
        try:
            result=youtube("POST","liveChat/messages",{"part":"snippet"},payload)
            ok=bool(result.get("id"))
        except Exception as e:
            print("YOUTUBE_BOT_SEND_FAILED",type(e).__name__,str(e)[:100],flush=True)
            return False
        _last_send=time.monotonic()
        return ok

def hourly_tick():
    state_file=ROOT/"youtube-chat-hourly.json"
    stored=_read_json(state_file)
    instant=time.time()
    if not isinstance(stored.get("last_sent_at"),(int,float)):
        _atomic_json(state_file,{"last_sent_at":instant,"next_message_index":0})
        return
    if instant-float(stored["last_sent_at"])<PERIOD_SECONDS:return
    if not _chat_id:return
    # _chat_id is set only after the authenticated YouTube API confirmed the
    # broadcast is live. Ignore stale local "starting" health on otherwise
    # healthy active broadcasts.
    if _read_json(ROOT/"youtube-chat-bot-runtime.json").get("status")!="subscribed":return
    idx=int(stored.get("next_message_index") or 0)
    if send(INTERACTION_MESSAGES[idx%len(INTERACTION_MESSAGES)]):
        _atomic_json(state_file,{"last_sent_at":time.time(),"next_message_index":idx+1})
        print("YOUTUBE_BOT_90MIN_MESSAGE_SENT",SLOT,flush=True)


def hourly_loop():
    while True:
        try:hourly_tick()
        except Exception as e:print("YOUTUBE_BOT_HOURLY_ERROR",type(e).__name__,flush=True)
        time.sleep(20)


def process_event(item):
    snippet=item.snippet
    if snippet.type!=1 or not snippet.author_channel_id or not item.id:return
    chatter=str(snippet.author_channel_id)
    if chatter==CHANNEL_ID:return
    text=str(snippet.display_message or "").strip()
    if not text:return
    cmd=text.casefold()
    if cmd in COMMANDS:
        result=process_chat_message(STATE,platform=SLOT,user_id=chatter,message_id=item.id,text=cmd)
        reply=english_chat_reply(result)
        if reply:send(reply)
    elif text.startswith("!"):
        return
    else:
        result=plan_chat_reply(STATE,platform=SLOT,user_id=chatter,message_id=item.id,
                               text=text,chatbot_user_id=CHANNEL_ID,ai_enabled=False)
        if result.get("status")=="reply":send(result.get("text",""))


def receive_forever():
    global _chat_id
    cursor_file=ROOT/"youtube-chat-stream-cursor.json"
    while True:
        try:
            live_id=confirmed_chat()
            _chat_id=live_id
            runtime("subscribed",youtube_live_chat_id=live_id,google_api="streamList")
            saved=_read_json(cursor_file)
            cursor=str(saved.get("next_page_token") or "") if (
                saved.get("video_id")==VIDEO_ID and saved.get("chat_id")==live_id) else ""
            # Each gRPC session uses one access token. Reconnect before expiry.
            with grpc.secure_channel("dns:///youtube.googleapis.com:443",grpc.ssl_channel_credentials()) as channel:
                stub=stub_module.V3DataLiveChatMessageServiceStub(channel)
                req=pb2.LiveChatMessageListRequest(live_chat_id=live_id,part=["id","snippet"],
                    page_token=cursor)
                initial=not cursor
                for batch in stub.StreamList(req,metadata=(("authorization","Bearer "+token()),),timeout=3300):
                    if batch.offline_at:raise RuntimeError("youtube_chat_offline")
                    if batch.next_page_token:
                        _atomic_json(cursor_file,{"next_page_token":batch.next_page_token,
                                                  "video_id":VIDEO_ID,"chat_id":live_id})
                    # On first connection, ignore the history to avoid replaying old commands.
                    if initial:
                        initial=False
                        continue
                    for item in batch.items:
                        try:process_event(item)
                        except Exception as exc:
                            print("YOUTUBE_BOT_MESSAGE_ERROR",type(exc).__name__,str(exc)[:100],flush=True)
                raise RuntimeError("youtube_stream_ended")
        except KeyboardInterrupt:return
        except Exception as exc:
            _chat_id=""
            runtime("reconnecting",reason=str(exc)[:100])
            print("YOUTUBE_BOT_RECONNECTING",SLOT,type(exc).__name__,str(exc)[:100],flush=True)
            time.sleep(30)


def main():
    if SLOT not in SLOTS or VIDEO_ID!=SLOTS[SLOT]:
        raise RuntimeError("video/slot mismatch; only the two approved Peter Lofi broadcasts may be used")
    if not re.fullmatch(r"[A-Za-z0-9_-]{11}",VIDEO_ID):raise RuntimeError("invalid video id")
    if "--preflight" in sys.argv:
        live_id=confirmed_chat()
        print("YOUTUBE_BOT_PREFLIGHT_OK",SLOT,"chat_id_len="+str(len(live_id)),
              "mode=read-only",flush=True)
        return
    ROOT.mkdir(parents=True,exist_ok=True)
    runtime("starting")
    threading.Thread(target=hourly_loop,daemon=True).start()
    receive_forever()


if __name__=="__main__":
    try:main()
    except Exception as exc:
        print("YOUTUBE_BOT_FATAL",type(exc).__name__,str(exc)[:160],flush=True)
        sys.exit(1)
