"""Twitch EventSub websocket bot, independent from any live encoder.

No OAuth access or refresh token is loaded here: the local MediaForge bridge
owns OAuth, session subscription, token rotation and chat posting.
"""
from __future__ import annotations
import asyncio
import fcntl
import json
import os
import random
import signal
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlparse
from websockets.asyncio.client import connect

from chat_requests import COMMANDS, _atomic_json, _read_json, english_chat_reply, process_chat_message
from bot_conversation import plan_chat_reply

STATE=Path(os.environ.get("CHAT_STATE_ROOT","/state"))
BRIDGE=os.environ.get("TWITCH_BOT_BRIDGE_URL","http://127.0.0.1:8790/api/oauth/twitch/bot").rstrip("/")
SECRET=os.environ.get("TWITCH_BOT_BRIDGE_TOKEN","")
COMMANDS_ENABLED=os.environ.get("TWITCH_BOT_COMMANDS_ENABLED","0")=="1"
CONVERSATION_ENABLED=os.environ.get("TWITCH_BOT_CONVERSATION_ENABLED","1")=="1"
WS_URI="wss://eventsub.wss.twitch.tv/ws"
MIN_REPLY_INTERVAL=8.0
HOURLY_INTERACTION_SECONDS=90*60
_last_reply=0.0
_send_lock=asyncio.Lock()
HOURLY_MESSAGES=(
    "Hey everyone! 🎮 What are you playing or working on right now? Drop it in chat!",
    "How are the vibes tonight? 🎧 Type !song to see what's playing, or tell us how your day is going!",
    "Quick check-in! 🌙 Are you gaming, studying, or just relaxing with PeterLofi Radio?",
    "Your soundtrack, your vibe! 🎵 What's one game or project you want to finish today?",
    "Thanks for hanging out with PeterLofi! ❤️ Where are you tuning in from?",
    "It's music break time! 🎶 What's your favorite late-night gaming memory?",
)


def bridge(endpoint,payload):
    if len(SECRET)<48:
        raise RuntimeError("missing private bot bridge token")
    request=urllib.request.Request(
        BRIDGE+"/"+endpoint,data=json.dumps(payload).encode(),
        headers={"authorization":"Bearer "+SECRET,"content-type":"application/json"},
        method="POST"
    )
    try:
        with urllib.request.urlopen(request,timeout=16) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        raise RuntimeError("MediaForge bridge HTTP "+str(exc.code)) from None


def valid_websocket_uri(uri):
    try:
        parsed=urlparse(uri)
        return parsed.scheme=="wss" and parsed.hostname=="eventsub.wss.twitch.tv" and parsed.port in (None,443)
    except Exception:
        return False


def load_health():
    p=STATE/"twitch"/"chat-bot-runtime.json"
    p.parent.mkdir(parents=True,exist_ok=True)
    return p


def runtime_info(status,**details):
    from chat_requests import _atomic_json
    _atomic_json(load_health(),{
        "service":"peterlofi-twitch-chatbot",
        "status":status,
        "updated_at":time.time(),
        "commands_enabled":COMMANDS_ENABLED,
        "conversation_enabled":CONVERSATION_ENABLED,
        **details
    })


async def api(endpoint,payload):
    return await asyncio.to_thread(bridge,endpoint,payload)


async def send(text):
    global _last_reply
    msg=" ".join(str(text or "").split())[:430]
    if not msg:
        return False
    # Serialize replies with hourly announcements to respect Twitch chat limits.
    async with _send_lock:
        remaining=MIN_REPLY_INTERVAL-(time.monotonic()-_last_reply)
        if remaining>0:
            await asyncio.sleep(remaining)
        result=await api("send",{"message":msg})
        _last_reply=time.monotonic()
        if not result.get("ok"):
            print("TWITCH_BOT_SEND_REJECTED",flush=True)
            return False
        return True


def hour_state():
    return STATE/"twitch"/"chat-hourly-interaction.json"


def choose_hourly_message(index):
    return HOURLY_MESSAGES[index % len(HOURLY_MESSAGES)]


def live_for_interaction():
    health=_read_json(STATE/"twitch"/"health.json")
    desired=_read_json(STATE/"twitch"/"desired.json")
    return health.get("status")=="live" and desired.get("desired")=="live"


async def hourly_interaction_tick(now=None):
    """Send at most one automatic message per 90 minutes while Twitch is live."""
    if not live_for_interaction():
        return "offline"
    now=time.time() if now is None else float(now)
    path=hour_state()
    path.parent.mkdir(parents=True,exist_ok=True)
    stored=_read_json(path)
    if not isinstance(stored.get("last_sent_at"),(int,float)):
        _atomic_json(path,{"last_sent_at":now,"next_message_index":0})
        return "scheduled"
    if now-float(stored["last_sent_at"])<HOURLY_INTERACTION_SECONDS:
        return "waiting"
    index=int(stored.get("next_message_index") or 0)
    # Unsuccessful sends are not recorded and are retried at the next check.
    if await send(choose_hourly_message(index)):
        _atomic_json(path,{"last_sent_at":time.time() if now is None else now,
                           "next_message_index":index+1})
        print("TWITCH_BOT_HOURLY_INTERACTION_SENT",flush=True)
        return "sent"
    return "retry"


async def hourly_interactions():
    """Persist delivery time across WebSocket reconnects and container restarts."""
    while True:
        await hourly_interaction_tick()
        await asyncio.sleep(15)


def allow_command_notice(platform,user_id,now=None):
    """Limit repeated rejection notices; Twitch uses its own chat Slow Mode.

    Accepted commands, current song, and freeze confirmations always reply.
    """
    now=time.time() if now is None else float(now)
    folder=STATE/platform
    folder.mkdir(parents=True,exist_ok=True)
    path=folder/"chat-bot-notices.json"
    lockpath=folder/"chat-bot-notices.lock"
    with lockpath.open("a+b") as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        ledger=_read_json(path)
        viewers={str(k):float(v) for k,v in (ledger.get("viewers") or {}).items()
                 if isinstance(v,(int,float)) and 0<=now-v<60}
        if user_id in viewers:
            return False
        viewers[user_id]=now
        _atomic_json(path,{"viewers":viewers})
        return True


async def on_message(event,broadcaster_id):
    msg=event.get("message") or {}
    chatter=str(event.get("chatter_user_id") or "")
    message_id=str(event.get("message_id") or "")
    text=str(msg.get("text") or "").strip()
    if not chatter or not message_id or chatter==broadcaster_id or not text:
        return
    # EventSub events are independently verified by Twitch over WSS, not
    # simulated from public incoming HTTP/webhooks.
    cmd=text.casefold()
    if cmd in COMMANDS:
        if not COMMANDS_ENABLED and cmd!="!song":
            # Never promise !freeze protection before the running AudioEngine
            # has been updated and independently verified.
            return
        result=await asyncio.to_thread(
            process_chat_message,STATE,platform="twitch",
            user_id=chatter,message_id=message_id,text=cmd
        )
        status=result.get("status")
        # Accepted actions and freeze confirmations always get an answer.
        # Previously cooldown/no_song/not_live responses were dropped silently.
        reportable=("accepted","now_playing","frozen_now","frozen","already_frozen",
                    "cooldown","station_cooldown","no_song","not_live")
        denied=("cooldown","station_cooldown","no_song","not_live")
        if status in reportable:
            if status not in denied or allow_command_notice("twitch",chatter):
                answer=english_chat_reply(result)
                if answer:
                    await send(answer)
        return
    if not CONVERSATION_ENABLED:
        return
    result=await asyncio.to_thread(plan_chat_reply,STATE,platform="twitch",user_id=chatter,
                                   message_id=message_id,text=text,
                                   chatbot_user_id=broadcaster_id,ai_enabled=False)
    if result.get("status")=="reply":
        await send(result.get("text",""))


async def connected_loop():
    url=WS_URI
    # Twitch can redirect sessions during network maintenance. The temporary
    # reconnect URL must be authenticated to the official EventSub hostname.
    while True:
        interaction_task=None
        try:
            # When Twitch asks to reconnect, EventSub migrates subscriptions
            # to the new session. We must not create a duplicate subscription.
            migrating=(url!=WS_URI)
            async with connect(url,open_timeout=15,ping_interval=20,
                               ping_timeout=20,max_size=1024*1024) as websocket:
                print("TWITCH_BOT_WS_CONNECTED",flush=True)
                runtime_info("websocket_connected")
                next_url=None
                broadcaster_id=""
                async for raw in websocket:
                    packet=json.loads(raw)
                    meta=packet.get("metadata") or {}
                    typ=meta.get("message_type")
                    payload=packet.get("payload") or {}
                    if typ=="session_welcome":
                        session=payload.get("session") or {}
                        session_id=str(session.get("id") or "")
                        if migrating:
                            result=await api("status",{})
                            if not result.get("connected"):
                                raise RuntimeError("Twitch OAuth not connected after EventSub migration")
                            broadcaster_id=str(result["broadcaster_id"])
                        else:
                            result=await api("subscribe",{"session_id":session_id})
                            if not result.get("ok"):
                                raise RuntimeError("Twitch EventSub subscription rejected")
                            broadcaster_id=str(result["broadcaster_id"])
                        runtime_info("subscribed",subscription_active=True)
                        print("TWITCH_BOT_EVENTSUB_SUBSCRIBED",flush=True)
                        if interaction_task is None or interaction_task.done():
                            interaction_task=asyncio.create_task(hourly_interactions())
                    elif typ=="notification" and broadcaster_id:
                        sub=payload.get("subscription") or {}
                        if sub.get("type")!="channel.chat.message":
                            continue
                        event=payload.get("event") or {}
                        if str(event.get("broadcaster_user_id"))!=broadcaster_id:
                            continue
                        try:
                            await on_message(event,broadcaster_id)
                        except Exception as exc:
                            print("TWITCH_BOT_MESSAGE_ERROR",type(exc).__name__,flush=True)
                    elif typ=="session_keepalive" and broadcaster_id:
                        runtime_info("subscribed",subscription_active=True)
                    elif typ=="session_reconnect":
                        new_uri=str((payload.get("session") or {}).get("reconnect_url") or "")
                        if valid_websocket_uri(new_uri):
                            next_url=new_uri
                            break
                    elif typ=="revocation":
                        raise RuntimeError("Twitch EventSub subscription revoked")
                url=next_url or WS_URI
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            runtime_info("reconnecting",reason=type(exc).__name__)
            print("TWITCH_BOT_RECONNECTING",type(exc).__name__,flush=True)
            url=WS_URI
            await asyncio.sleep(5+random.random()*5)
        finally:
            if interaction_task is not None:
                interaction_task.cancel()
                try:
                    await interaction_task
                except asyncio.CancelledError:
                    pass


if __name__=="__main__":
    try:
        if len(SECRET)<48:
            raise RuntimeError("bridge token missing")
        if not BRIDGE.startswith("http://127.0.0.1:8790/api/oauth/twitch/bot"):
            raise RuntimeError("bridge must stay on localhost")
        print("TWITCH_CHATBOT_START", "music_controls="+str(COMMANDS_ENABLED),
              "mention_replies="+str(CONVERSATION_ENABLED),flush=True)
        asyncio.run(connected_loop())
    except KeyboardInterrupt:
        pass
    except Exception as exc:
        print("TWITCH_CHATBOT_FATAL",type(exc).__name__,flush=True)
        sys.exit(1)
