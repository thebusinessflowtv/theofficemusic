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
        data=urllib.parse.urlencode(
            {
                "client_id": os.environ["YOUTUBE_CLIENT_ID"],
                "client_secret": os.environ["YOUTUBE_CLIENT_SECRET"],
                "refresh_token": os.environ["YOUTUBE_REFRESH_TOKEN"],
                "grant_type": "refresh_token",
            }
        ).encode("utf-8"),
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
            method="POST",
            data=body,
            headers=json_headers,
        )

    def yt_put(resource, params, body):
        return request_json(
            f"https://www.googleapis.com/youtube/v3/{resource}?{urllib.parse.urlencode(params)}",
            method="PUT",
            data=body,
            headers=json_headers,
        )

    def yt_delete(resource, params):
        return request_json(
            f"https://www.googleapis.com/youtube/v3/{resource}?{urllib.parse.urlencode(params)}",
            method="DELETE",
            headers=base_headers,
        )

    channel_items = yt_get(
        "channels", {"part": "id,contentDetails,snippet", "mine": "true", "maxResults": "1"}
    ).get("items", [])
    if not channel_items or channel_items[0].get("id") != os.environ["YOUTUBE_CHANNEL_ID"]:
        raise RuntimeError("Authenticated channel mismatch")
    channel = channel_items[0]
    uploads_pid = (
        ((channel.get("contentDetails") or {}).get("relatedPlaylists") or {}).get("uploads")
    )
    if not uploads_pid:
        raise RuntimeError("Channel uploads playlist not found")

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

    upload_items = list_playlist_items(uploads_pid, "contentDetails")
    upload_ids = [
        (item.get("contentDetails") or {}).get("videoId") for item in upload_items
    ]
    upload_ids = [video_id for video_id in upload_ids if video_id]

    videos = []
    for index in range(0, len(upload_ids), 50):
        chunk = upload_ids[index : index + 50]
        if not chunk:
            continue
        videos.extend(
            yt_get(
                "videos",
                {
                    "part": "id,snippet,status,contentDetails",
                    "id": ",".join(chunk),
                    "maxResults": "50",
                },
            ).get("items", [])
        )

    public_videos = [
        video
        for video in videos
        if (video.get("status") or {}).get("privacyStatus") == "public"
    ]
    public_ids = {video["id"] for video in public_videos}
    print(f"PUBLIC_VIDEO_COUNT={len(public_videos)}")

    specs = {
        "all_music": {
            "title": "Peter Lofi — All Music 🎧",
            "description": "Every public Peter Lofi release in one place — original lofi music, chill beats, focus mixes and late-night sounds for study, work and relaxing.",
        },
        "lofi_mixes": {
            "title": "Lofi Mixes 🎧 | Peter Lofi",
            "description": "Long-form Peter Lofi mixes for uninterrupted focus, study, work, reading and relaxing.",
        },
        "late_night": {
            "title": "Late Night Lofi 🌙 | Peter Lofi",
            "description": "Nighttime lofi, neon city moods and calm after-dark beats from Peter Lofi.",
        },
        "rainy_cozy": {
            "title": "Rainy & Cozy Lofi 🌧️ | Peter Lofi",
            "description": "Rainy-night and cozy-window lofi by Peter Lofi for calm focus, reading and relaxing.",
        },
        "focus_study": {
            "title": "Focus & Study 📚 | Peter Lofi",
            "description": "Peter Lofi music for studying, working, coding, concentrating and staying in the zone.",
        },
        "deep_focus": {
            "title": "Deep Focus 💻 | Peter Lofi",
            "description": "Minimal, steady Peter Lofi beats designed for deep concentration and long productive sessions.",
        },
        "city_nights": {
            "title": "City Nights 🌃 | Peter Lofi",
            "description": "Urban skyline, rooftop and neon-city lofi from the Peter Lofi universe.",
        },
        "chill_relax": {
            "title": "Chill & Relax ☕ | Peter Lofi",
            "description": "Warm, mellow Peter Lofi tracks for coffee breaks, reading, relaxing and slow evenings.",
        },
    }

    legacy_aliases = {
        "all_music": ["All Songs 🎧 | The Office Music"],
        "lofi_mixes": ["Lounge House Mix"],
        "late_night": ["Late Night Chill 🎧 | The Office Music"],
        "rainy_cozy": ["Deep Focus Lofi 🎧 | The Office Music"],
        "focus_study": ["Chill Office 🎧 | The Office Music"],
        "deep_focus": ["Focus & Coding 🎧 | The Office Music"],
        "city_nights": ["Lounge House 🎧 | The Office Music"],
        "chill_relax": ["Lofi Lounge 🎧 | The Office Music"],
    }

    def list_channel_playlists():
        rows = []
        page = ""
        while True:
            params = {
                "part": "id,snippet,contentDetails,status",
                "mine": "true",
                "maxResults": "50",
            }
            if page:
                params["pageToken"] = page
            payload = yt_get("playlists", params)
            rows.extend(payload.get("items", []))
            page = payload.get("nextPageToken") or ""
            if not page:
                return rows

    current = list_channel_playlists()
    by_title = {
        ((playlist.get("snippet") or {}).get("title") or ""): playlist
        for playlist in current
    }
    playlist_ids = {}
    used_ids = set()

    for key, spec in specs.items():
        found = by_title.get(spec["title"])
        if not found:
            for alias in legacy_aliases.get(key, []):
                candidate = by_title.get(alias)
                if candidate and candidate.get("id") not in used_ids:
                    found = candidate
                    break

        if found:
            pid = found["id"]
            used_ids.add(pid)
            snippet = found.get("snippet") or {}
            title_matches = (snippet.get("title") or "") == spec["title"]
            description_matches = (snippet.get("description") or "") == spec["description"]
            if not (title_matches and description_matches):
                yt_put(
                    "playlists",
                    {"part": "snippet"},
                    {
                        "id": pid,
                        "snippet": {
                            "title": spec["title"],
                            "description": spec["description"],
                            "defaultLanguage": "en",
                        },
                    },
                )
                print("UPDATED_PLAYLIST", pid, spec["title"])
            else:
                print("PLAYLIST_OK", pid, spec["title"])
        else:
            created = yt_post(
                "playlists",
                {"part": "snippet,status"},
                {
                    "snippet": {
                        "title": spec["title"],
                        "description": spec["description"],
                        "defaultLanguage": "en",
                    },
                    "status": {"privacyStatus": "public"},
                },
            )
            pid = created["id"]
            used_ids.add(pid)
            print("CREATED_PLAYLIST", pid, spec["title"])
        playlist_ids[key] = pid

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

    category_context = {
        "lofi_mixes": "Long-form lofi for uninterrupted focus, study, work and relaxation.",
        "late_night": "Late-night lofi for quiet city hours, headphones and after-dark focus.",
        "rainy_cozy": "Cozy lofi for rainy windows, reading, calm work and slow evenings.",
        "focus_study": "Lofi music for studying, working, coding and staying focused.",
        "deep_focus": "Steady lofi for deep concentration and distraction-free sessions.",
        "city_nights": "Urban lofi inspired by rooftops, city lights and late-night skylines.",
        "chill_relax": "Warm lofi for relaxing, reading, coffee breaks and easy listening.",
    }
    category_hashtag = {
        "lofi_mixes": "#LofiMix",
        "late_night": "#LateNightLofi",
        "rainy_cozy": "#RainyLofi",
        "focus_study": "#StudyMusic",
        "deep_focus": "#DeepFocus",
        "city_nights": "#CityLofi",
        "chill_relax": "#ChillMusic",
    }
    category_tags = {
        "lofi_mixes": ["lofi mix", "1 hour lofi", "long lofi mix"],
        "late_night": ["late night lofi", "night lofi", "neon lofi"],
        "rainy_cozy": ["rainy lofi", "cozy lofi", "rain music"],
        "focus_study": ["study lofi", "focus lofi", "coding music", "productivity music"],
        "deep_focus": ["deep focus music", "concentration music"],
        "city_nights": ["city lofi", "urban lofi", "rooftop lofi"],
        "chill_relax": ["chill lofi", "relaxing lofi", "coffee shop music"],
    }
    base_tags = [
        "Peter Lofi",
        "PeterLofiSounds",
        "lofi",
        "lofi music",
        "lofi beats",
        "focus music",
        "study music",
        "work music",
        "chill music",
        "background music",
        "relaxing music",
    ]

    def optimized_description(video, category):
        snippet = video.get("snippet") or {}
        chapters = extract_chapters(snippet.get("description") or "")
        parts = [
            "🎧 Peter Lofi — original lofi sounds for focus, study, work and late-night sessions.",
            "",
            category_context[category],
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
            f"#PeterLofi #Lofi #LofiMusic #LofiBeats #FocusMusic {category_hashtag[category]}",
        ]
        return "\n".join(parts)[:5000]

    report = []
    desired_category = {}
    for video in public_videos:
        video_id = video["id"]
        snippet = video.get("snippet") or {}
        category = classify(video)
        desired_category[video_id] = category
        old_title = snippet.get("title") or ""
        new_title = optimized_title(old_title)
        new_description = optimized_description(video, category)
        tags = []
        seen = set()
        for tag in base_tags + category_tags[category]:
            lowered = tag.lower()
            if lowered not in seen:
                tags.append(tag)
                seen.add(lowered)
        while len(",".join(tags)) > 430:
            tags.pop()

        old_tags = snippet.get("tags") or []
        needs_update = (
            old_title != new_title
            or (snippet.get("description") or "") != new_description
            or old_tags != tags
        )
        if needs_update:
            new_snippet = {
                "title": new_title,
                "description": new_description,
                "categoryId": snippet.get("categoryId") or "10",
                "tags": tags,
            }
            if snippet.get("defaultLanguage"):
                new_snippet["defaultLanguage"] = snippet["defaultLanguage"]
            yt_put("videos", {"part": "snippet"}, {"id": video_id, "snippet": new_snippet})
            print("UPDATED_VIDEO", video_id, "->", new_title)
        else:
            print("VIDEO_OK", video_id, new_title)

        report.append(
            {
                "video_id": video_id,
                "old_title": old_title,
                "new_title": new_title,
                "related_playlist_key": category,
                "related_playlist_title": specs[category]["title"],
                "duration_seconds": iso_duration_seconds(
                    (video.get("contentDetails") or {}).get("duration")
                ),
                "published_at": snippet.get("publishedAt"),
            }
        )

    membership = {}
    for key, pid in playlist_ids.items():
        items = list_playlist_items(pid)
        kept = set()
        for item in items:
            item_id = item.get("id")
            video_id = (item.get("contentDetails") or {}).get("videoId") or (
                ((item.get("snippet") or {}).get("resourceId") or {}).get("videoId")
            )
            should_keep = bool(
                video_id in public_ids
                and (key == "all_music" or desired_category.get(video_id) == key)
            )
            if should_keep:
                kept.add(video_id)
            elif item_id:
                yt_delete("playlistItems", {"id": item_id})
                print("REMOVED_PLAYLIST_ITEM", item_id, video_id or "")
        membership[key] = kept

    def add_video(key, video_id):
        if video_id in membership[key]:
            return
        yt_post(
            "playlistItems",
            {"part": "snippet"},
            {
                "snippet": {
                    "playlistId": playlist_ids[key],
                    "resourceId": {"kind": "youtube#video", "videoId": video_id},
                }
            },
        )
        membership[key].add(video_id)
        print("ADDED", video_id, "->", specs[key]["title"])

    for video in public_videos:
        video_id = video["id"]
        add_video("all_music", video_id)
        add_video(desired_category[video_id], video_id)

    playlists = []
    for playlist in list_channel_playlists():
        pid = playlist.get("id")
        snippet = playlist.get("snippet") or {}
        status = playlist.get("status") or {}
        items = []
        for item in list_playlist_items(pid):
            item_snippet = item.get("snippet") or {}
            content = item.get("contentDetails") or {}
            thumbs = item_snippet.get("thumbnails") or {}
            items.append(
                {
                    "playlist_item_id": item.get("id"),
                    "video_id": content.get("videoId")
                    or ((item_snippet.get("resourceId") or {}).get("videoId")),
                    "title": item_snippet.get("title") or "",
                    "position": int(item_snippet.get("position") or 0),
                    "thumbnail_url": (
                        (thumbs.get("medium") or thumbs.get("high") or thumbs.get("default") or {}).get("url")
                        or ""
                    ),
                    "video_owner_channel_id": item_snippet.get("videoOwnerChannelId") or "",
                    "privacy_status": (item.get("status") or {}).get("privacyStatus") or "",
                }
            )
        items.sort(key=lambda row: row["position"])
        thumbs = snippet.get("thumbnails") or {}
        playlists.append(
            {
                "id": pid,
                "title": snippet.get("title") or "",
                "description": snippet.get("description") or "",
                "thumbnail_url": (
                    (thumbs.get("medium") or thumbs.get("high") or thumbs.get("default") or {}).get("url")
                    or ""
                ),
                "item_count": len(items),
                "privacy_status": status.get("privacyStatus") or "",
                "items": items,
            }
        )

    playlists.sort(key=lambda row: row["title"].lower())
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    control = pathlib.Path("control")
    control.mkdir(parents=True, exist_ok=True)
    (control / "youtube-playlists.json").write_text(
        json.dumps(
            {
                "channel_id": os.environ["YOUTUBE_CHANNEL_ID"],
                "brand": BRAND,
                "handle_url": HANDLE_URL,
                "generated_at": now,
                "playlist_count": len(playlists),
                "playlists": playlists,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    (control / "peter-lofi-channel-audit.json").write_text(
        json.dumps(
            {
                "brand": BRAND,
                "handle_url": HANDLE_URL,
                "generated_at": now,
                "public_video_count": len(public_videos),
                "managed_playlists": {
                    key: {"id": playlist_ids[key], **specs[key]} for key in specs
                },
                "videos": report,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    print("DONE: organized", len(public_videos), "public videos")


if __name__ == "__main__":
    main()
