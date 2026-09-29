import random
from copy import deepcopy


def _join(values):
    return ", ".join(str(v).strip() for v in values if str(v).strip())


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

    prompt = (
        "TrackType: Music, VocalType: Instrumental. "
        f"A {style} instrumental for focused office and home-office listening, {bpm} BPM. "
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
        "style": style,
        "bpm": bpm,
        "instruments": selected,
        "mood": selected_mood,
        "duration_seconds": int(generation["track_duration_seconds"]),
    }
    return prompt, negative_prompt, metadata
