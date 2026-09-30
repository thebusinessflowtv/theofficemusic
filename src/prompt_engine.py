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

    style_pool = dna.get("style_pool") or ["polished modern instrumental lofi"]
    style = rng.choice(style_pool)
    bpm = rng.randint(int(dna.get("bpm_min", 82)), int(dna.get("bpm_max", 102)))

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

    # Series profiles intentionally override only the musical attributes that need to change.
    # Keep sensible defaults here so a missing optional DNA field can never abort a generation.
    groove = dna.get("groove") or (
        "steady relaxed groove with subtle swing, a consistent pulse and smooth transitions, "
        "hypnotic enough for focus without becoming aggressive"
    )
    percussion = dna.get("percussion") or (
        "clean restrained beat, soft kick, crisp but gentle hats and no aggressive fills"
    )
    bass = dna.get("bass") or (
        "warm rounded bassline, controlled and supportive, never overpowering the mix"
    )
    melody_density = dna.get("melody_density") or (
        "low to medium-low, minimalist motifs with gradual variation and little distraction"
    )
    brightness = dna.get("brightness") or (
        "smooth polished top end with warm low mids and no harsh treble"
    )
    arrangement = dna.get("arrangement") or (
        "continuous instrumental arrangement with natural evolution, restrained transitions, "
        "no abrupt drops and no sudden genre changes"
    )
    production = dna.get("production") or (
        "premium clean stereo mix, warm, spacious and unobtrusive for long listening sessions"
    )

    prompt = (
        "TrackType: Music, VocalType: Instrumental. "
        f"A {style} instrumental for {listening_context}, {bpm} BPM. "
        f"Mood: {_join(selected_mood)}. "
        f"Instruments: {_join(selected)}. "
        f"Groove: {groove}. "
        f"Percussion: {percussion}. "
        f"Bass: {bass}. "
        f"Melodic density: {melody_density}. "
        f"Brightness: {brightness}. "
        f"Arrangement: {arrangement}. "
        f"Production: {production}. "
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
