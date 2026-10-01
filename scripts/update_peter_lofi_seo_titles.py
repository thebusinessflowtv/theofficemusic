#!/usr/bin/env python3
import base64, json, os, time, urllib.error, urllib.request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

REPO = os.environ["GITHUB_REPOSITORY"]
TOKEN = os.environ["GH_TOKEN"]
API = f"https://api.github.com/repos/{REPO}"

LIVE_TITLES = {
    "fe6e6fd3-fd9c-4265-b12d-8eb50c0b52fc": "rainy lofi radio 🌧️ beats to study/work/relax to | Peter Lofi",
    "5f54bf8e-d700-4a67-bafd-79b1dd047be5": "deep house radio 💻 music to work/study/focus to | Peter Lofi",
}

SERIES_TITLES = {
    "office-productivity": "1 hour lofi for work 💻 beats for focus & productivity | Peter Lofi",
    "late-night-office": "1 hour late night lofi 🌙 beats to work/study/focus to | Peter Lofi",
    "coffee-shop": "1 hour coffee shop lofi ☕ beats to study/work/relax to | Peter Lofi",
    "rooftop-office": "1 hour city lofi 🌆 chill beats to work/study/focus to | Peter Lofi",
    "rainy-office": "1 hour rainy lofi 🌧️ beats to study/work/relax to | Peter Lofi",
    "sunday-morning-work": "1 hour morning lofi ☀️ beats to relax/work/study to | Peter Lofi",
    "deep-focus-coding": "1 hour lofi for coding 💻 beats for deep focus & flow | Peter Lofi",
    "luxury-office": "1 hour deep house for work ✨ lounge music for focus | Peter Lofi",
    "work-from-home": "1 hour lofi for work from home 🏠 focus & productivity beats | Peter Lofi",
    "night-shift": "1 hour night lofi 🌃 beats to work/focus/study to | Peter Lofi",
}

TITLE_MATCHERS = [
    ("office flow", "office-productivity"),
    ("late night office", "late-night-office"),
    ("coffee shop", "coffee-shop"),
    ("rooftop office", "rooftop-office"),
    ("rainy office", "rainy-office"),
    ("sunday morning work", "sunday-morning-work"),
    ("deep focus coding", "deep-focus-coding"),
    ("luxury office", "luxury-office"),
    ("work from home", "work-from-home"),
    ("night shift", "night-shift"),
]


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
            item["seo_title_pattern"] = "duration + search term first; listener intent second; Peter Lofi last"
            changed += 1
    data["seo_strategy"] = {
        "updated": "2026-10-01",
        "principle": "search-first titles: actual genre/context + radio/beats/music + listener intent; brand at end",
        "core_intents": ["study", "work", "focus", "relax", "coding", "productivity"],
    }
    put_text(path, json.dumps(data, ensure_ascii=False, indent=2) + "\n", "seo: search-first titles for Peter Lofi series")
    print(f"Updated {changed} future video title presets")


def match_series(title):
    t = (title or "").lower()
    for needle, key in TITLE_MATCHERS:
        if needle in t:
            return key
    return None


def update_existing_uploads(yt):
    """Retitle existing series uploads when their old series name can be matched safely."""
    channel = yt.channels().list(part="contentDetails", mine=True).execute().get("items") or []
    if not channel:
        print("No channel found; skipping existing-video SEO")
        return
    uploads = channel[0]["contentDetails"]["relatedPlaylists"]["uploads"]
    ids = []
    token = None
    while len(ids) < 50:
        resp = yt.playlistItems().list(part="contentDetails", playlistId=uploads, maxResults=50, pageToken=token).execute()
        ids.extend(x["contentDetails"]["videoId"] for x in resp.get("items") or [])
        token = resp.get("nextPageToken")
        if not token:
            break
    active_ids = set()
    for sid in LIVE_TITLES:
        try:
            _, raw = get_content(f"control/live-results/{sid}.json")
            active_ids.add(json.loads(raw).get("youtube_broadcast_id"))
        except Exception:
            pass
    ids = [x for x in ids if x and x not in active_ids]
    updated = 0
    for pos in range(0, len(ids), 50):
        videos = yt.videos().list(part="snippet", id=",".join(ids[pos:pos+50])).execute().get("items") or []
        for video in videos:
            snippet = video["snippet"]
            key = match_series(snippet.get("title"))
            if not key:
                continue
            new_title = SERIES_TITLES[key][:100]
            if snippet.get("title") == new_title:
                continue
            snippet["title"] = new_title
            yt.videos().update(part="snippet", body={"id": video["id"], "snippet": snippet}).execute()
            updated += 1
            print(f"Retitled existing video {video['id']}: {new_title}")
    print(f"Existing uploaded videos retitled: {updated}")


def main():
    yt = youtube()
    update_live_broadcasts(yt)
    update_series_titles()
    update_existing_uploads(yt)


if __name__ == "__main__":
    main()
