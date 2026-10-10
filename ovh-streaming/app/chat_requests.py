"""PeterLofi chat-command business logic. Only call with verified platform chat events.

The Twitch/Kick adapters must authenticate platform events before invoking this
module. No chat polling or streaming publisher processes are started here.
"""
from __future__ import annotations

import fcntl
import json
import os
import re
import time
import uuid
from pathlib import Path

SLOTS = frozenset(("twitch", "kick"))
COMMANDS = frozenset(("!skip", "!song", "!back", "!freeze"))
USER_COOLDOWN_SECONDS = 180
# Protect a station against many viewers causing rapid successive track changes.
TRACK_CHANGE_GLOBAL_SECONDS = 60
DEDUP_SECONDS = 24 * 3600


def _read_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (FileNotFoundError, ValueError, OSError):
        return {}


def _atomic_json(path: Path, obj: dict) -> None:
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with tmp.open("x", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False, separators=(",", ":"))
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def _active_freeze(state: dict, song: dict) -> dict | None:
    freeze = state.get("freeze")
    if not isinstance(freeze, dict):
        return None
    if not freeze.get("track_id") or not song.get("track_id"):
        return None
    return freeze if str(freeze["track_id"]) == str(song["track_id"]) else None


def process_chat_message(
    state_root: str | Path,
    *,
    platform: str,
    user_id: str,
    message_id: str,
    text: str,
    now: float | None = None,
) -> dict:
    """Return accepted/cooldown/frozen/etc. without restarting encoders.

    Per-user 180s across ALL four commands, including !song. Twitch and Kick
    are isolated. Freeze prevents !skip and !back from all users (including
    older queued controls once the AudioEngine is patched). Natural track-end
    removes the freeze; cooldown stays anchored to last *accepted* request.
    """
    if platform not in SLOTS:
        raise ValueError("unsupported platform")
    user_id = str(user_id).strip()
    message_id = str(message_id).strip()
    if not user_id or not message_id or len(user_id) > 160 or len(message_id) > 160:
        raise ValueError("valid platform-scoped user_id and message_id required")
    command = re.sub(r"\s+", " ", str(text or "").strip()).casefold()
    if command not in COMMANDS:
        return {"status": "ignored"}

    root = Path(state_root) / platform
    root.mkdir(parents=True, exist_ok=True)
    instant = float(time.time() if now is None else now)

    with (root / "chat-bot.lock").open("a+b") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        state_path = root / "chat-bot-state.json"
        state = _read_json(state_path)
        seen = {str(k): float(v) for k, v in (state.get("seen") or {}).items()
                if isinstance(v, (float, int)) and 0 <= instant - v < DEDUP_SECONDS}
        users = {str(k): float(v) for k, v in (state.get("users") or {}).items()
                 if isinstance(v, (float, int)) and 0 <= instant - v < USER_COOLDOWN_SECONDS}
        if message_id in seen:
            return {"status": "duplicate"}
        seen[message_id] = instant

        song = _read_json(root / "now-playing.json")
        freeze = _active_freeze(state, song)
        if not freeze and state.get("freeze"):
            state.pop("freeze", None)  # Stale after natural/automatic transition.

        def persist():
            state.update({"users": users, "seen": seen})
            _atomic_json(state_path, state)

        if command in ("!skip", "!back") and freeze:
            persist()
            return {"status": "frozen", "title": str(song.get("title") or "")}
        if command == "!freeze" and freeze:
            persist()
            return {"status": "already_frozen", "title": str(song.get("title") or "")}

        remaining_user = max(0.0, USER_COOLDOWN_SECONDS - (instant - users.get(user_id, -1e20)))
        if remaining_user:
            persist()
            return {"status": "cooldown", "retry_after": int(remaining_user + 0.999)}

        if command in ("!skip", "!back"):
            remaining_global = max(0.0, TRACK_CHANGE_GLOBAL_SECONDS - (
                instant - float(state.get("last_change", -1e20))
            ))
            if remaining_global:
                persist()
                return {"status": "station_cooldown", "retry_after": int(remaining_global + 0.999)}

        health = _read_json(root / "health.json")
        desired = _read_json(root / "desired.json")
        if health.get("status") != "live" or desired.get("desired") != "live":
            persist()
            return {"status": "not_live"}

        if command == "!freeze":
            track_id = str(song.get("track_id") or "").strip()
            if not track_id:
                persist()
                return {"status": "no_song"}
            state["freeze"] = {"track_id": track_id, "user_id": user_id,
                               "set_at": instant}
            users[user_id] = instant
            persist()
            return {"status": "frozen_now", "title": str(song.get("title") or "")}

        if command == "!song":
            users[user_id] = instant
            persist()
            return {"status": "now_playing", "title": str(song.get("title") or ""),
                    "artists": str(song.get("artists") or "")}

        action = {"!skip": "skip", "!back": "previous"}[command]
        queue = root / "audio-commands"
        queue.mkdir(parents=True, exist_ok=True)
        command_id = uuid.uuid4().hex
        _atomic_json(queue / f"{time.time_ns():020d}-{command_id}.json", {
            "id": command_id, "action": action, "requested_at": instant,
            "source": f"chat-bot-{platform}",
        })
        state["last_change"] = instant
        users[user_id] = instant
        persist()
        return {"status": "accepted", "action": action, "command_id": command_id}


def clear_freeze_after_track_change(
    state_root: str | Path, platform: str, old_track_id: str
) -> bool:
    """Called by AudioEngine when the old track has actually ended or changed.

    Never resets user cooldown. Locked against concurrent !freeze requests.
    """
    if platform not in SLOTS:
        raise ValueError("unsupported platform")
    root = Path(state_root) / platform
    with (root / "chat-bot.lock").open("a+b") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        state_path = root / "chat-bot-state.json"
        state = _read_json(state_path)
        freeze = state.get("freeze")
        if not isinstance(freeze, dict) or str(freeze.get("track_id")) != str(old_track_id):
            return False
        state.pop("freeze", None)
        _atomic_json(state_path, state)
        return True


def english_chat_reply(result: dict) -> str:
    """Short English responses suitable for either broadcaster chat adapter."""
    status = str(result.get("status") or "")
    title = str(result.get("title") or "Unknown track")[:100]
    if status == "accepted":
        return "Next song requested! 🎵" if result.get("action") == "skip" else "Going back to the previous song! 🎵"
    if status == "now_playing":
        artists = str(result.get("artists") or "")[:80]
        return f"Now playing: {title}" + (f" — {artists}" if artists else "")
    if status == "frozen_now":
        return f"Song frozen: {title}. No skipping or going back until it ends. 🔒"
    if status in ("frozen", "already_frozen"):
        return "This song is frozen. Wait until it finishes. 🔒"
    if status == "cooldown":
        return f"You can use another command in {int(result.get('retry_after') or 0)} seconds."
    if status == "station_cooldown":
        return f"Please wait {int(result.get('retry_after') or 0)} seconds before another song change."
    if status == "not_live":
        return "The radio isn't live right now."
    if status == "no_song":
        return "No song information is available yet."
    return ""
