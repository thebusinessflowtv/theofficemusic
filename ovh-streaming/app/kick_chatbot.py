"""Kick webhook inbox bot; independent of Kick/Twitch RTMP publishers.

The OVH local API verifies Kick RSA event signatures before queueing messages.
Only this isolated container reads chat and writes Kick audio commands.
"""
from __future__ import annotations

import json
import os
import random
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

from chat_requests import COMMANDS,_atomic_json,_read_json,process_chat_message,english_chat_reply
from bot_conversation import plan_chat_reply

ROOT=Path(os.environ.get("CHAT_STATE_ROOT","/state"))
BRIDGE=os.environ.get("KICK_BOT_BRIDGE_URL","http://127.0.0.1:8790/api/oauth/kick/bot").rstrip("/")
SECRET=os.environ.get("KICK_BOT_BRIDGE_TOKEN","")
COMMANDS_ENABLED=os.environ.get("KICK_BOT_COMMANDS_ENABLED","1")=="1"
CONVERSATION_ENABLED=os.environ.get("KICK_BOT_CONVERSATION_ENABLED","1")=="1"
HOURLY=3600
MESSAGES=(
    "Hey Kick chat! 🎮 What are you playing today? Drop it in chat!",
    "Welcome to PeterLofi Radio! 🎧 Type !song to check the music or !freeze to lock this track.",
    "Quick check-in! 🌙 Are you gaming, studying or relaxing with us today?",
    "Thanks for listening! 🎵 Where are you tuning in from?",
    "Your radio, your vibe! 🎮 What's the last game you couldn't stop playing?",
)
_last_sent=0.0


def bridge(endpoint, payload):
    if len(SECRET)<48 or not BRIDGE.startswith("http://127.0.0.1:8790/api/oauth/kick/bot"):
        raise RuntimeError("Kick bridge must use private local credentials and endpoint")
    req=urllib.request.Request(BRIDGE+"/"+endpoint,
        data=json.dumps(payload).encode(),
        headers={"authorization":"Bearer "+SECRET,"content-type":"application/json"},method="POST")
    try:
        with urllib.request.urlopen(req,timeout=20) as r:
            return json.load(r)
    except urllib.error.HTTPError as exc:
        raise RuntimeError("Kick bridge HTTP "+str(exc.code)) from None


def runtime(status,**kwargs):
    _atomic_json(ROOT/"kick"/"chat-bot-runtime.json",{
        "service":"peterlofi-kick-chatbot","status":status,
        "commands_enabled":COMMANDS_ENABLED and status=="subscribed",
        "conversation_enabled":CONVERSATION_ENABLED,
        "updated_at":time.time(),**kwargs})


def send(message):
    global _last_sent
    text=" ".join(str(message or "").split())[:430]
    if not text:return False
    wait=8-(time.monotonic()-_last_sent)
    if wait>0:time.sleep(wait)
    result=bridge("send",{"message":text})
    _last_sent=time.monotonic()
    return result.get("ok") is True


def live():
    h=_read_json(ROOT/"kick"/"health.json")
    d=_read_json(ROOT/"kick"/"desired.json")
    return h.get("status")=="live" and d.get("desired")=="live"


def hourly():
    if not live():return
    path=ROOT/"kick"/"chat-hourly-interaction.json"
    last=_read_json(path)
    instant=time.time()
    if not isinstance(last.get("last_sent_at"),(int,float)):
        _atomic_json(path,{"last_sent_at":instant,"next_message_index":0})
        return
    if instant-float(last["last_sent_at"])<HOURLY:return
    i=int(last.get("next_message_index") or 0)
    if send(MESSAGES[i%len(MESSAGES)]):
        _atomic_json(path,{"last_sent_at":time.time(),"next_message_index":i+1})
        print("KICK_BOT_HOURLY_INTERACTION_SENT",flush=True)


def consume(event,broadcaster_id):
    user=str(event.get("user_id") or "")
    eid=str(event.get("message_id") or "")
    msg=str(event.get("content") or "").strip()
    if not user or not eid or not msg or user==broadcaster_id:return
    command=msg.casefold()
    if command in COMMANDS:
        if not COMMANDS_ENABLED and command!="!song":return
        result=process_chat_message(ROOT,platform="kick",user_id=user,message_id=eid,text=msg)
        text=english_chat_reply(result)
        if text:send(text)
        return
    if not CONVERSATION_ENABLED:return
    result=plan_chat_reply(ROOT,platform="kick",user_id=user,message_id=eid,
                            text=msg,chatbot_user_id=broadcaster_id,ai_enabled=False)
    if result.get("status")=="reply":
        send(result["text"])


def main():
    if len(SECRET)<48:raise RuntimeError("Kick bridge secret missing")
    if not BRIDGE.startswith("http://127.0.0.1:8790/api/oauth/kick/bot"):
        raise RuntimeError("Bot endpoint must be local")
    runtime("starting")
    while True:
        try:
            packet=bridge("poll",{})
            broadcaster_id=str(packet.get("broadcaster_id") or "")
            if not broadcaster_id or not packet.get("ok"):raise RuntimeError("Kick bot authorization missing")
            runtime("subscribed",subscription_active=True)
            for event in packet.get("events") or []:
                try:consume(event,broadcaster_id)
                except Exception as err:
                    print("KICK_BOT_EVENT_ERROR",type(err).__name__,flush=True)
                finally:
                    # Never remove an event that was not delivered to the bot
                    # unless authenticated processing has been attempted.
                    bridge("ack",{"id":str(event["id"])})
            hourly()
            time.sleep(3)
        except KeyboardInterrupt:return
        except Exception as err:
            runtime("reconnecting",reason=type(err).__name__)
            print("KICK_BOT_RECONNECTING",type(err).__name__,flush=True)
            time.sleep(10+random.random()*3)


if __name__=="__main__":
    try:main()
    except Exception as e:
        print("KICK_BOT_FATAL",type(e).__name__,flush=True)
        sys.exit(1)
