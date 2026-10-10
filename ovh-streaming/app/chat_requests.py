"""PeterLofi per-platform chat requests. Call only with authenticated chat events.
Authentication/transport adapters run independently from the live publishers.
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
SKIP_COOLDOWN_SECONDS = 60
USER_COOLDOWN_SECONDS = 60
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


def process_chat_message(
    state_root: str | Path,
    *,
    platform: str,
    user_id: str,
    message_id: str,
    text: str,
    now: float | None = None,
) -> dict:
    """Process verified chat text; never stop/restart an RTMP publisher.

    !skip: per-user AND per-platform global cooldown of 60 seconds.
    !song: read track metadata only. Ordinary chat is unaffected.
    """
    if platform not in SLOTS:
        raise ValueError("unsupported platform")
    user_id = str(user_id).strip()
    message_id = str(message_id).strip()
    if not user_id or not message_id or len(user_id) > 160 or len(message_id) > 160:
        raise ValueError("valid platform-scoped user_id and message_id required")
    command = re.sub(r"\s+", " ", str(text or "").strip()).casefold()
    if command not in ("!skip", "!pular", "!song", "!musica", "!música"):
        return {"status": "ignored"}
    root = Path(state_root) / platform
    root.mkdir(parents=True, exist_ok=True)
    instant = float(time.time() if now is None else now)
    if command in ("!song", "!musica", "!música"):
        song = _read_json(root / "now-playing.json")
        return {"status": "now_playing", "title": str(song.get("title") or ""), "artists": str(song.get("artists") or "")}

    with (root / "chat-bot.lock").open("a+b") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        state_path = root / "chat-bot-state.json"
        state = _read_json(state_path)
        seen = {str(k): float(v) for k, v in (state.get("seen") or {}).items()
                if isinstance(v, (float, int)) and instant - v < DEDUP_SECONDS}
        users = {str(k): float(v) for k, v in (state.get("users") or {}).items()
                 if isinstance(v, (float, int)) and instant - v < USER_COOLDOWN_SECONDS}
        if message_id in seen:
            return {"status": "duplicate"}
        seen[message_id] = instant
        last_user = users.get(user_id, -1e20)
        last_global = float(state.get("last_global", -1e20))
        remaining_user = max(0, USER_COOLDOWN_SECONDS - (instant - last_user))
        remaining_global = max(0, SKIP_COOLDOWN_SECONDS - (instant - last_global))
        if remaining_user > 0 or remaining_global > 0:
            state.update({"users": users, "seen": seen})
            _atomic_json(state_path, state)
            return {"status": "cooldown", "retry_after": int(max(remaining_user, remaining_global) + 0.999)}

        health = _read_json(root / "health.json")
        desired = _read_json(root / "desired.json")
        if health.get("status") != "live" or desired.get("desired") != "live":
            state.update({"users": users, "seen": seen})
            _atomic_json(state_path, state)
            return {"status": "not_live"}

        command_dir = root / "audio-commands"
        command_dir.mkdir(parents=True, exist_ok=True)
        command_id = uuid.uuid4().hex
        command_name = f"{time.time_ns():020d}-{command_id}.json"
        _atomic_json(command_dir / command_name, {
            "id": command_id,
            "action": "skip",
            "requested_at": instant,
            "source": f"chat-bot-{platform}",
        })
        users[user_id] = instant
        state.update({"last_global": instant, "users": users, "seen": seen})
        _atomic_json(state_path, state)
        return {"status": "accepted", "command_id": command_id}
