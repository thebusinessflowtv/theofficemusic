import random
from copy import deepcopy


def _join(values):
    return ", ".join(str(v).strip() for v in values if str(v).strip())


TITLE_LEFT = [
    "Midnight", "Velvet", "After Hours", "Glass", "Soft Focus", "City", "Quiet",
    "Neon", "Morning", "Blue Hour", "Lobby", "Downtown", "Golden", "Late Check-In",
    "Window Seat", "Executive", "Studio", "Northbound", "Sunday", "Silver"
]
TITLE_RIGHT = [
    "Avenue", "Lobby", "Elevator", "Desk", "Transit", "Suite", "Coffee", "Windows",
    "Routine", "Boulevard", "Workspace", "Escalator", "District", "Notes", "Hours",
    "Floor", "Hallway", "Skyline", "Meeting", "Afterglow"
]


def build_prompt(profile: dict, track_index: int, rng: random.Random) -> tuple[str, str, dict]:
    dna = deepcopy(profile["music_dna"])
    generation = profile["generation"]

    style = rng.choice(dna["style_pool"])
    bpm = rng.randint(int(dna["bpm_min"]), int(dna["bpm_max"]))

    instruments = dna.get("preferred_instruments", [])
    selected = instruments[:]
    rng.shuffle(selected)
    selected = selected[: min(4, len(selected))]

    mood = dna.get("mood", [])
    selected_mood = mood[:]
    rng.shuffle(selected_mood)
    selected_mood = selected_mood[: min(3, len(selected_mood))]

    title_left = dna.get("title_left") or TITLE_LEFT
    title_right = dna.get("title_right") or TITLE_RIGHT
    title = f"{rng.choice(title_left)} {rng.choice(title_right)}"
    listening_context = dna.get("listening_context") or "focused office and home-office listening"

    prompt = (
        "TrackType: Music, VocalType: Instrumental. "
        f"A {style} instrumental for {listening_context}, {bpm} BPM. "
        f"Mood: {_join(selected_mood)}. "
        f"Instruments: {_join(selected)}. "
        f"Groove: {dna['groove']}. "
        f"Percussion: {dna['percussion']}. "
        f"Bass: {dna['bass']}. "
        f"Melodic density: {dna['melody_density']}. "
        f"Brightness: {dna['brightness']}. "
        f"Arrangement: {dna['arrangement']}. "
        f"Production: {dna['production']}. "
        "Instrumental only, no intelligible singing, no spoken words. "
        "Elegant background music that remains interesting without demanding attention."
    )

    negative_prompt = _join(profile.get("negative_prompt", []))

    metadata = {
        "track_index": track_index,
        "title": title,
        "style": style,
        "bpm": bpm,
        "instruments": selected,
        "mood": selected_mood,
        "series": profile.get("channel", {}).get("concept", ""),
        "duration_seconds": int(generation["track_duration_seconds"]),
    }
    return prompt, negative_prompt, metadata
