#!/usr/bin/env python3
import base64, json, os, time, urllib.error, urllib.request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

REPO = os.environ["GITHUB_REPOSITORY"]
TOKEN = os.environ["GH_TOKEN"]
API = f"https://api.github.com/repos/{REPO}"

LIVE_TITLES = {
    "fe6e6fd3-fd9c-4265-b12d-8eb50c0b52fc": "rainy lofi radio 🌧️ beats to study/work/relax to | Peter Lofi",
    "5f54bf8e-d700-4a67-bafd-79b1dd047be5": "lofi hip hop radio 📚 beats to study/work/focus to | Peter Lofi",
}

SERIES_TITLES = {
    "office-productivity": "lofi for work & productivity 💻 beats to focus to | Peter Lofi",
    "late-night-office": "late night lofi 🌙 beats to work/study/focus to | Peter Lofi",
    "coffee-shop": "coffee shop lofi ☕ beats to study/work/relax to | Peter Lofi",
    "rooftop-office": "city lofi 🌆 chill beats to work/study/focus to | Peter Lofi",
    "rainy-office": "rainy lofi 🌧️ beats to study/work/relax to | Peter Lofi",
    "sunday-morning-work": "morning lofi ☀️ beats to relax/work/study to | Peter Lofi",
    "deep-focus-coding": "lofi for coding 💻 beats to focus/program to | Peter Lofi",
    "luxury-office": "chill house for work ✨ lounge beats to focus to | Peter Lofi",
    "work-from-home": "lofi for work from home 🏠 beats to focus/productivity to | Peter Lofi",
    "night-shift": "night lofi 🌃 beats to work/focus/study to | Peter Lofi",
}


def gh(method, path, body=None, allow_404=False):
    headers = {
        "Authorization": f"Bearer {TOKEN}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "Peter-Lofi-SEO",
    }
    data = None
    if body is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(body).encode()
    req = urllib.request.Request(f"{API}/{path.lstrip('/')}", data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            raw = r.read()
            return json.loads(raw.decode()) if raw else {}
    except urllib.error.HTTPError as e:
        if allow_404 and e.code == 404:
            return None
        raise RuntimeError(f"GitHub {method} {path}: HTTP {e.code}: {e.read().decode('utf-8','replace')[-800:]}")


def get_content(path):
    obj = gh("GET", f"contents/{path}")
    raw = base64.b64decode((obj.get("content") or "").replace("\n", ""))
    return obj, raw.decode("utf-8")


def put_text(path, text, message):
    last = None
    for attempt in range(1, 8):
        try:
            obj = gh("GET", f"contents/{path}")
            body = {
                "message": message,
                "branch": "main",
                "sha": obj["sha"],
                "content": base64.b64encode(text.encode("utf-8")).decode(),
            }
            return gh("PUT", f"contents/{path}", body)
        except Exception as exc:
            last = exc
            time.sleep(attempt)
    raise RuntimeError(f"Could not update {path}: {last}")


def youtube():
    creds = Credentials(
        token=None,
        refresh_token=os.environ["YOUTUBE_REFRESH_TOKEN"],
        token_uri="https://oauth2.googleapis.com/token",
        client_id=os.environ["YOUTUBE_CLIENT_ID"],
        client_secret=os.environ["YOUTUBE_CLIENT_SECRET"],
        scopes=["https://www.googleapis.com/auth/youtube"],
    )
    return build("youtube", "v3", credentials=creds, cache_discovery=False)


def update_live_broadcasts(yt):
    for session_id, title in LIVE_TITLES.items():
        result_path = f"control/live-results/{session_id}.json"
        _, raw = get_content(result_path)
        result = json.loads(raw)
        bid = result.get("youtube_broadcast_id")
        if not bid:
            raise RuntimeError(f"{session_id}: no broadcast id")
        items = yt.liveBroadcasts().list(part="snippet", id=bid).execute().get("items") or []
        if not items:
            raise RuntimeError(f"Broadcast {bid} not found")
        snippet = items[0]["snippet"]
        snippet["title"] = title[:100]
        yt.liveBroadcasts().update(part="snippet", body={"id": bid, "snippet": snippet}).execute()
        print(f"Updated YouTube broadcast {bid}: {title}")

        queue_path = f"control/youtube-live-queue/{session_id}.json"
        _, qraw = get_content(queue_path)
        q = json.loads(qraw)
        q["title"] = title
        q["seo_title_updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        put_text(queue_path, json.dumps(q, ensure_ascii=False, indent=2) + "\n", f"seo: update live title {session_id}")


def update_series_titles():
    path = "config/peter_lofi_series.json"
    _, raw = get_content(path)
    data = json.loads(raw)
    changed = 0
    for item in data.get("series", []):
        key = item.get("key")
        if key in SERIES_TITLES:
            item["title"] = SERIES_TITLES[key]
            item["seo_title_pattern"] = "search-term first; use-case intent second; Peter Lofi last"
            changed += 1
    data["seo_strategy"] = {
        "updated": "2026-10-01",
        "principle": "search-first lofi titles: genre/context + radio or beats + listener intent; brand at end",
        "core_intents": ["study", "work", "focus", "relax", "coding", "productivity"],
    }
    put_text(path, json.dumps(data, ensure_ascii=False, indent=2) + "\n", "seo: search-first titles for Peter Lofi series")
    print(f"Updated {changed} future video title presets")


def main():
    yt = youtube()
    update_live_broadcasts(yt)
    update_series_titles()


if __name__ == "__main__":
    main()
