#!/usr/bin/env python3
"""Low-quota control-plane helpers for MediaForge live runners.

The live encoder is the data plane. GitHub is only coordination. Reads prefer
raw.githubusercontent.com so they do not consume the Actions installation REST
quota. Critical coordination writes can use git transport, which is independent
from the REST contents API rate limit that previously killed a live encoder.
"""
import json
import os
import pathlib
import subprocess
import time
import urllib.error
import urllib.request


def raw_get(repo, path, user_agent="MediaForge-Live-Control", timeout=12):
    url = f"https://raw.githubusercontent.com/{repo}/main/{path}?ts={int(time.time() * 1000)}"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": user_agent, "Cache-Control": "no-cache"})
        with urllib.request.urlopen(req, timeout=timeout) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None
        print(f"::warning::raw control read HTTP {exc.code} for {path}", flush=True)
        return None
    except Exception as exc:
        print(f"::warning::raw control read unavailable for {path}: {exc}", flush=True)
        return None


def _run(cmd, check=True):
    return subprocess.run(cmd, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, check=check)


def git_put_json(path, payload, message, attempts=8):
    """Persist one JSON state file through git transport instead of REST Contents API.

    Intended for low-frequency, critical handoff markers. It deliberately stages
    only the requested path, so build media and other runner files are untouched.
    """
    target = pathlib.Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    content = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"

    try:
        _run(["git", "config", "user.name", "github-actions[bot]"])
        _run(["git", "config", "user.email", "41898282+github-actions[bot]@users.noreply.github.com"])
    except Exception as exc:
        print(f"::warning::git control-plane identity setup failed: {exc}", flush=True)
        return False

    last = None
    for attempt in range(1, attempts + 1):
        try:
            # Synchronize before editing so the critical marker is based on current main.
            fetch = _run(["git", "fetch", "origin", "main"], check=False)
            if fetch.returncode != 0:
                raise RuntimeError(fetch.stdout[-1500:])
            rebase = _run(["git", "rebase", "origin/main"], check=False)
            if rebase.returncode != 0:
                _run(["git", "rebase", "--abort"], check=False)
                # Keep the runner source branch usable and retry from latest main.
                reset = _run(["git", "reset", "--hard", "origin/main"], check=False)
                if reset.returncode != 0:
                    raise RuntimeError((rebase.stdout + "\n" + reset.stdout)[-1500:])

            target.write_text(content, encoding="utf-8")
            add = _run(["git", "add", "--", path], check=False)
            if add.returncode != 0:
                raise RuntimeError(add.stdout[-1500:])

            diff = _run(["git", "diff", "--cached", "--quiet", "--", path], check=False)
            if diff.returncode == 0:
                print(f"git control marker already current: {path}", flush=True)
                return True

            commit = _run(["git", "commit", "-m", message, "--", path], check=False)
            if commit.returncode != 0:
                raise RuntimeError(commit.stdout[-1500:])

            push = _run(["git", "push", "origin", "HEAD:main"], check=False)
            if push.returncode == 0:
                print(f"git control marker persisted: {path}", flush=True)
                return True

            last = RuntimeError(push.stdout[-1500:])
            print(f"git control push retry {attempt}/{attempts} for {path}: {push.stdout[-500:]}", flush=True)
            time.sleep(min(6, attempt))
        except Exception as exc:
            last = exc
            print(f"git control write retry {attempt}/{attempts} for {path}: {exc}", flush=True)
            time.sleep(min(6, attempt))

    print(f"::warning::git control write failed for {path}: {last}", flush=True)
    return False


def is_critical_handoff_path(path):
    prefixes = (
        "control/live-ready/",
        "control/live-takeover/",
        "control/live-active/",
        "control/kick-prewarm/",
    )
    return path.startswith(prefixes)
