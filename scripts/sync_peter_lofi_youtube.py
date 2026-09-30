import datetime
import json
import os
import pathlib
import re
import urllib.error
import urllib.parse
import urllib.request

BRAND = "Peter Lofi"
HANDLE_URL = "https://www.youtube.com/@PeterLofiSounds"

SPECS = {
    "all_music": {
        "title": "Peter Lofi — All Music 🎧",
        "description": "Every public Peter Lofi release in one place — original lofi music, chill beats, focus mixes and late-night sounds for study, work and relaxing.",
        "legacy": ["All Songs 🎧 | The Office Music"],
    },
    "lofi_mixes": {
        "title": "Lofi Mixes 🎧 | Peter Lofi",
        "description": "Long-form Peter Lofi mixes for uninterrupted focus, study, work, reading and relaxing.",
        "legacy": ["Lounge House Mix"],
    },
    "late_night": {
        "title": "Late Night Lofi 🌙 | Peter Lofi",
        "description": "Nighttime lofi, neon city moods and calm after-dark beats from Peter Lofi.",
        "legacy": ["Late Night Chill 🎧 | The Office Music"],
    },
    "rainy_cozy": {
        "title": "Rainy & Cozy Lofi 🌧️ | Peter Lofi",
        "description": "Rainy-night and cozy-window lofi by Peter Lofi for calm focus, reading and relaxing.",
        "legacy": ["Deep Focus Lofi 🎧 | The Office Music"],
    },
    "focus_study": {
        "title": "Focus & Study 📚 | Peter Lofi",
        "description": "Peter Lofi music for studying, working, coding, concentrating and staying in the zone.",
        "legacy": ["Chill Office 🎧 | The Office Music"],
    },
    "deep_focus": {
        "title": "Deep Focus 💻 | Peter Lofi",
        "description": "Minimal, steady Peter Lofi beats designed for deep concentration and long productive sessions.",
        "legacy": ["Focus & Coding 🎧 | The Office Music"],
    },
    "city_nights": {
        "title": "City Nights 🌃 | Peter Lofi",
        "description": "Urban skyline, rooftop and neon-city lofi from the Peter Lofi universe.",
        "legacy": ["Lounge House 🎧 | The Office Music"],
    },
    "chill_relax": {
        "title": "Chill & Relax ☕ | Peter Lofi",
        "description": "Warm, mellow Peter Lofi tracks for coffee breaks, reading, relaxing and slow evenings.",
        "legacy": ["Lofi Lounge 🎧 | The Office Music"],
    },
}

CATEGORY_CONTEXT = {
    "lofi_mixes": "Long-form lofi for uninterrupted focus, study, work and relaxation.",
    "late_night": "Late-night lofi for quiet city hours, headphones and after-dark focus.",
    "rainy_cozy": "Cozy lofi for rainy windows, reading, calm work and slow evenings.",
    "focus_study": "Lofi music for studying, working, coding and staying focused.",
    "deep_focus": "Steady lofi for deep concentration and distraction-free sessions.",
    "city_nights": "Urban lofi inspired by rooftops, city lights and late-night skylines.",
    "chill_relax": "Warm lofi for relaxing, reading, coffee breaks and easy listening.",
}

CATEGORY_HASHTAG = {
    "lofi_mixes": "#LofiMix",
    "late_night": "#LateNightLofi",
    "rainy_cozy": "#RainyLofi",
    "focus_study": "#StudyMusic",
    "deep_focus": "#DeepFocus",
    "city_nights": "#CityLofi",
    "chill_relax": "#ChillMusic",
}

CATEGORY_TAGS = {
    "lofi_mixes": ["lofi mix", "1 hour lofi", "long lofi mix"],
    "late_night": ["late night lofi", "night lofi", "neon lofi"],
    "rainy_cozy": ["rainy lofi", "cozy lofi", "rain music"],
    "focus_study": ["study lofi", "focus lofi", "coding music", "productivity music"],
    "deep_focus": ["deep focus music", "concentration music"],
    "city_nights": ["city lofi", "urban lofi", "rooftop lofi"],
    "chill_relax": ["chill lofi", "relaxing lofi", "coffee shop music"],
}

BASE_TAGS = [
    "Peter Lofi", "PeterLofiSounds", "lofi", "lofi music", "lofi beats",
    "focus music", "study music", "work music", "chill music",
    "background music", "relaxing music",
]


def request_json(url, method="GET", data=None, headers=None):
    payload = None if data is None else json.dumps(data, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=payload, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=60) as response:
            raw = response.read()
            return json.loads(raw.decode("utf-8")) if raw else {}
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")
        raise RuntimeError(f"HTTP {exc.code} {method} {url}: {body}") from exc


def oauth_token():
    req = urllib.request.Request(
        "https://oauth2.googleapis.com/token",
        data=urllib.parse.urlencode({
            "client_id": os.environ["YOUTUBE_CLIENT_ID"],
            "client_secret": os.environ["YOUTUBE_CLIENT_SECRET"],
            "refresh_token": os.environ["YOUTUBE_REFRESH_TOKEN"],
            "grant_type": "refresh_token",
        }).encode("utf-8"),
        method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            payload = json.load(response)
    except urllib.error.HTTPError as exc:
        raise RuntimeError(
            f"OAuth failed HTTP {exc.code}: {exc.read().decode('utf-8', 'replace')}"
        ) from exc
    token = payload.get("access_token")
    if not token:
        raise RuntimeError("OAuth response missing access_token")
    return token


def iso_duration_seconds(value):
    if not value:
        return 0
    match = re.fullmatch(r"P(?:(\d+)D)?T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", value)
    if not match:
        return 0
    days, hours, minutes, seconds = [int(x or 0) for x in match.groups()]
    return days * 86400 + hours * 3600 + minutes * 60 + seconds


def classify(video):
    snippet = video.get("snippet") or {}
    text = ((snippet.get("title") or "") + " " + (snippet.get("description") or "")).lower()
    duration = iso_duration_seconds((video.get("contentDetails") or {}).get("duration"))
    if duration >= 15 * 60:
        return "lofi_mixes"
    if any(word in text for word in ["rain", "rainy", "storm", "thunder"]):
        return "rainy_cozy"
    if any(word in text for word in ["midnight", "late night", "night", "moon", "neon", "after dark"]):
        return "late_night"
    if any(word in text for word in ["city", "urban", "skyline", "rooftop", "downtown", "manhattan"]):
        return "city_nights"
    if any(word in text for word in ["deep focus", "concentration", "concentrate"]):
        return "deep_focus"
    if any(word in text for word in ["focus", "study", "studying", "work", "coding", "code", "office", "productivity"]):
        return "focus_study"
    return "chill_relax"


def optimized_title(old):
    base = (old or "Lofi Music").replace("The Office Music", BRAND).strip()
    base = re.sub(r"\s*🎧\s*$", "", base).strip()
    if BRAND.lower() not in base.lower():
        suffix = f" | {BRAND}"
        max_base = 100 - len(suffix) - 2
        base = base[:max_base].rstrip(" |-–—") + suffix
    return (base + " 🎧")[:100]


def extract_chapters(description):
    chapters = []
    for line in (description or "").splitlines():
        if re.match(r"^\s*\d{1,2}:\d{2}(?::\d{2})?\s+\S", line):
            chapters.append(line.strip())
    return chapters[:80]


def optimized_description(video, category):
    snippet = video.get("snippet") or {}
    chapters = extract_chapters(snippet.get("description") or "")
    parts = [
        "🎧 Peter Lofi — original lofi sounds for focus, study, work and late-night sessions.",
        "",
        CATEGORY_CONTEXT[category],
        "",
        "Put on your headphones, slow things down and stay awhile.",
        "",
        "🎧 Subscribe to Peter Lofi:",
        HANDLE_URL,
    ]
    if chapters:
        parts += ["", "Tracklist / Chapters:", *chapters]
    parts += [
        "",
        "Original music by Peter Lofi.",
        "© 2026 Peter Lofi. All rights reserved.",
        "",
        f"#PeterLofi #Lofi #LofiMusic #LofiBeats #FocusMusic {CATEGORY_HASHTAG[category]}",
    ]
    return "\n".join(parts)[:5000]


def main():
    access = oauth_token()
    base_headers = {"Authorization": f"Bearer {access}"}
    json_headers = {
        "Authorization": f"Bearer {access}",
        "Content-Type": "application/json; charset=utf-8",
    }

    def yt_get(resource, params):
        return request_json(
            f"https://www.googleapis.com/youtube/v3/{resource}?{urllib.parse.urlencode(params)}",
            headers=base_headers,
        )

    def yt_post(resource, params, body):
        return request_json(
            f"https://www.googleapis.com/youtube/v3/{resource}?{urllib.parse.urlencode(params)}",
            method="POST", data=body, headers=json_headers,
        )

    def yt_put(resource, params, body):
        return request_json(
            f"https://www.googleapis.com/youtube/v3/{resource}?{urllib.parse.urlencode(params)}",
            method="PUT", data=body, headers=json_headers,
        )

    def yt_delete(resource, params):
        return request_json(
            f"https://www.googleapis.com/youtube/v3/{resource}?{urllib.parse.urlencode(params)}",
            method="DELETE", headers=base_headers,
        )

    def list_channel_playlists():
        rows = []
        page = ""
        while True:
            params = {"part": "id,snippet,contentDetails,status", "mine": "true", "maxResults": "50"}
            if page:
                params["pageToken"] = page
            payload = yt_get("playlists", params)
            rows.extend(payload.get("items", []))
            page = payload.get("nextPageToken") or ""
            if not page:
                return rows

    def list_playlist_items(pid, parts="id,snippet,contentDetails,status"):
        rows = []
        page = ""
        while True:
            params = {"part": parts, "playlistId": pid, "maxResults": "50"}
            if page:
                params["pageToken"] = page
            payload = yt_get("playlistItems", params)
            rows.extend(payload.get("items", []))
            page = payload.get("nextPageToken") or ""
            if not page:
                return rows

    channel_items = yt_get(
        "channels", {"part": "id,contentDetails,snippet", "mine": "true", "maxResults": "1"}
    ).get("items", [])
    if not channel_items or channel_items[0].get("id") != os.environ["YOUTUBE_CHANNEL_ID"]:
        raise RuntimeError("Authenticated channel mismatch")
    uploads_pid = (((channel_items[0].get("contentDetails") or {}).get("relatedPlaylists") or {}).get("uploads"))
    if not uploads_pid:
        raise RuntimeError("Channel uploads playlist not found")

    upload_ids = [
        (item.get("contentDetails") or {}).get("videoId")
        for item in list_playlist_items(uploads_pid, "contentDetails")
    ]
    upload_ids = [video_id for video_id in upload_ids if video_id]

    videos = []
    for index in range(0, len(upload_ids), 50):
        chunk = upload_ids[index:index + 50]
        if chunk:
            videos.extend(yt_get("videos", {
                "part": "id,snippet,status,contentDetails",
                "id": ",".join(chunk),
                "maxResults": "50",
            }).get("items", []))

    public_videos = [
        video for video in videos
        if (video.get("status") or {}).get("privacyStatus") == "public"
    ]
    public_ids = {video["id"] for video in public_videos}
    desired_category = {video["id"]: classify(video) for video in public_videos}
    active_categories = set(desired_category.values())
    active_keys = {"all_music", *active_categories}
    print(f"PUBLIC_VIDEO_COUNT={len(public_videos)}")
    print("ACTIVE_PLAYLIST_KEYS=" + ",".join(sorted(active_keys)))

    # Resolve managed playlists. Keep only global + categories that actually contain public music.
    current = list_channel_playlists()
    title_to_playlists = {}
    for playlist in current:
        title = ((playlist.get("snippet") or {}).get("title") or "")
        title_to_playlists.setdefault(title, []).append(playlist)

    managed_titles = set()
    for spec in SPECS.values():
        managed_titles.add(spec["title"])
        managed_titles.update(spec["legacy"])

    playlist_ids = {}
    used_ids = set()

    # Delete managed playlists whose category currently has no public music.
    for key, spec in SPECS.items():
        if key in active_keys:
            continue
        candidate_titles = [spec["title"], *spec["legacy"]]
        for title in candidate_titles:
            for playlist in title_to_playlists.get(title, []):
                pid = playlist.get("id")
                if pid and pid not in used_ids:
                    yt_delete("playlists", {"id": pid})
                    used_ids.add(pid)
                    print("DELETED_EMPTY_PLAYLIST", pid, title)

    # Resolve/create only playlists that are actually needed.
    for key in ["all_music", *sorted(active_categories)]:
        spec = SPECS[key]
        candidates = []
        for title in [spec["title"], *spec["legacy"]]:
            candidates.extend(title_to_playlists.get(title, []))
        candidates = [p for p in candidates if p.get("id") not in used_ids]

        if candidates:
            found = candidates[0]
            pid = found["id"]
            used_ids.add(pid)
            snippet = found.get("snippet") or {}
            if ((snippet.get("title") or "") != spec["title"] or
                    (snippet.get("description") or "") != spec["description"]):
                yt_put("playlists", {"part": "snippet"}, {
                    "id": pid,
                    "snippet": {
                        "title": spec["title"],
                        "description": spec["description"],
                        "defaultLanguage": "en",
                    },
                })
                print("UPDATED_PLAYLIST", pid, spec["title"])
            else:
                print("PLAYLIST_OK", pid, spec["title"])

            # Remove duplicate managed playlists for the same category.
            for duplicate in candidates[1:]:
                duplicate_id = duplicate.get("id")
                if duplicate_id and duplicate_id not in used_ids:
                    yt_delete("playlists", {"id": duplicate_id})
                    used_ids.add(duplicate_id)
                    print("DELETED_DUPLICATE_PLAYLIST", duplicate_id)
        else:
            created = yt_post("playlists", {"part": "snippet,status"}, {
                "snippet": {
                    "title": spec["title"],
                    "description": spec["description"],
                    "defaultLanguage": "en",
                },
                "status": {"privacyStatus": "public"},
            })
            pid = created["id"]
            used_ids.add(pid)
            print("CREATED_PLAYLIST", pid, spec["title"])

        playlist_ids[key] = pid

    # Optimize metadata for current public videos.
    report = []
    for video in public_videos:
        vid = video["id"]
        snippet = video.get("snippet") or {}
        category = desired_category[vid]
        new_title = optimized_title(snippet.get("title") or "")
        new_desc = optimized_description(video, category)

        tags = []
        for tag in BASE_TAGS + CATEGORY_TAGS[category]:
            if tag.lower() not in {x.lower() for x in tags}:
                tags.append(tag)
        while len(",".join(tags)) > 430:
            tags.pop()

        old_tags = snippet.get("tags") or []
        needs_update = (
            (snippet.get("title") or "") != new_title
            or (snippet.get("description") or "") != new_desc
            or [x.lower() for x in old_tags] != [x.lower() for x in tags]
        )
        if needs_update:
            new_snippet = {
                "title": new_title,
                "description": new_desc,
                "categoryId": snippet.get("categoryId") or "10",
                "tags": tags,
            }
            if snippet.get("defaultLanguage"):
                new_snippet["defaultLanguage"] = snippet["defaultLanguage"]
            yt_put("videos", {"part": "snippet"}, {"id": vid, "snippet": new_snippet})
            print("UPDATED_VIDEO", vid, new_title)
        else:
            print("VIDEO_OK", vid, new_title)

        report.append({
            "video_id": vid,
            "title": new_title,
            "related_playlist_key": category,
            "related_playlist_title": SPECS[category]["title"],
            "duration_seconds": iso_duration_seconds((video.get("contentDetails") or {}).get("duration")),
            "published_at": snippet.get("publishedAt"),
        })

    # Clean active managed playlists and then add every public video to global + exactly one category.
    membership = {}
    for key, pid in playlist_ids.items():
        kept = set()
        for item in list_playlist_items(pid):
            item_id = item.get("id")
            vid = ((item.get("contentDetails") or {}).get("videoId")
                   or (((item.get("snippet") or {}).get("resourceId") or {}).get("videoId")))
            should_keep = bool(vid in public_ids and (key == "all_music" or desired_category.get(vid) == key))
            if should_keep and vid not in kept:
                kept.add(vid)
            elif item_id:
                yt_delete("playlistItems", {"id": item_id})
                print("REMOVED_STALE_PLAYLIST_ITEM", item_id, vid or "")
        membership[key] = kept

    def add_video(key, vid):
        if vid in membership[key]:
            return
        yt_post("playlistItems", {"part": "snippet"}, {
            "snippet": {
                "playlistId": playlist_ids[key],
                "resourceId": {"kind": "youtube#video", "videoId": vid},
            }
        })
        membership[key].add(vid)
        print("ADDED", vid, "->", SPECS[key]["title"])

    for video in public_videos:
        vid = video["id"]
        add_video("all_music", vid)
        add_video(desired_category[vid], vid)

    # Refresh the complete playlist snapshot consumed by MediaForge.
    playlists = []
    for playlist in list_channel_playlists():
        pid = playlist.get("id")
        snippet = playlist.get("snippet") or {}
        status = playlist.get("status") or {}
        items = []
        for item in list_playlist_items(pid):
            item_snippet = item.get("snippet") or {}
            item_details = item.get("contentDetails") or {}
            thumbs = item_snippet.get("thumbnails") or {}
            items.append({
                "playlist_item_id": item.get("id"),
                "video_id": item_details.get("videoId") or ((item_snippet.get("resourceId") or {}).get("videoId")),
                "title": item_snippet.get("title") or "",
                "position": int(item_snippet.get("position") or 0),
                "thumbnail_url": ((thumbs.get("medium") or thumbs.get("high") or thumbs.get("default") or {}).get("url") or ""),
                "privacy_status": (item.get("status") or {}).get("privacyStatus") or "",
            })
        items.sort(key=lambda row: row["position"])
        thumbs = snippet.get("thumbnails") or {}
        playlists.append({
            "id": pid,
            "title": snippet.get("title") or "",
            "description": snippet.get("description") or "",
            "thumbnail_url": ((thumbs.get("medium") or thumbs.get("high") or thumbs.get("default") or {}).get("url") or ""),
            "item_count": len(items),
            "privacy_status": status.get("privacyStatus") or "",
            "items": items,
        })

    playlists.sort(key=lambda row: row["title"].lower())
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    snapshot = {
        "channel_id": os.environ["YOUTUBE_CHANNEL_ID"],
        "brand": BRAND,
        "handle_url": HANDLE_URL,
        "generated_at": now,
        "playlist_count": len(playlists),
        "playlists": playlists,
    }
    pathlib.Path("control/youtube-playlists.json").write_text(
        json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    audit = {
        "brand": BRAND,
        "handle_url": HANDLE_URL,
        "generated_at": now,
        "public_video_count": len(public_videos),
        "active_managed_playlists": {key: {"id": playlist_ids[key], **SPECS[key]} for key in playlist_ids},
        "videos": report,
    }
    pathlib.Path("control/peter-lofi-channel-audit.json").write_text(
        json.dumps(audit, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print("DONE: organized", len(public_videos), "public videos into", len(playlist_ids), "managed playlists")


if __name__ == "__main__":
    main()
