#!/usr/bin/env python3
import argparse
import json
from pathlib import Path

ALT_INTROS = [
    "start with only a dry bass pulse for two bars, then add a rimshot before the full beat",
    "open with a single analog chord stab motif and no kick, then drop into the groove",
    "begin with gated toms and filtered noise, then reveal bass before hats",
    "start immediately with the hook but omit bass for the first four bars",
]
ALT_DRUMS = [
    "use a more syncopated kick map and fewer hats than the base profile",
    "use a straighter kick pattern but replace claps with a tight dry snare",
    "use a broken-electro kick pattern with offbeat percussion accents",
    "use a sparse backbeat with tom punctuation and no busy fills",
]
ALT_BASS = [
    "favor short offbeat bass stabs rather than a continuous pulse",
    "favor longer held bass notes with syncopated pickups",
    "favor octave movement with rests on strong beats",
    "favor a two-bar bass answer phrase rather than a one-bar loop",
]
ALT_LEAD = [
    "replace the main hook with a sparse mono synth phrase",
    "use chord stabs as the hook and keep the lead secondary",
    "use a digital mallet motif with no repeating arpeggio",
    "use a slowly evolving polysynth phrase rather than a pluck hook",
]

def build(index: int, variant_attempt: int) -> dict:
    queue = json.loads(Path("control/gta-vi-vice-city/queue.json").read_text(encoding="utf-8"))
    tracks = queue["generated_tracks"]
    if not (1 <= index <= len(tracks)):
        raise ValueError(f"index must be 1..{len(tracks)}")
    track = tracks[index - 1]
    if index == 1:
        raise ValueError("Ocean Drive Heat is preserved and must not be regenerated")

    left, right = track["title"].split(" ", 1)
    attempt_offset = max(0, variant_attempt - 1)

    intro = track["intro_identity"]
    drums = track["drum_identity"]
    bass = track["bass_identity"]
    lead = track["lead_identity"]
    if attempt_offset:
        intro += "; " + ALT_INTROS[(index + attempt_offset) % len(ALT_INTROS)]
        drums += "; " + ALT_DRUMS[(index * 3 + attempt_offset) % len(ALT_DRUMS)]
        bass += "; " + ALT_BASS[(index * 5 + attempt_offset) % len(ALT_BASS)]
        lead += "; " + ALT_LEAD[(index * 7 + attempt_offset) % len(ALT_LEAD)]

    profile = {
        "channel": {"name": "Peter Lofi", "concept": "GTA VI - Vice City — diverse revision"},
        "generation": {
            "model": "medium",
            "tracks_per_batch": 1,
            "track_duration_seconds": 300,
            "steps": 8,
            "cfg_scale": 1.0,
            "chunked_decode": True,
            "pure_text_to_audio": True,
        },
        "music_dna": {
            "instrumental_only": True,
            "energy": 8,
            "bpm_min": track["target_bpm"],
            "bpm_max": track["target_bpm"],
            "style_pool": [track["style"]],
            "mood": [x.strip() for x in track["mood"].split(",")],
            "preferred_instruments": [
                bass,
                drums,
                lead,
                track["texture_identity"],
            ],
            "listening_context": "a humid neon coastal night drive with retro-futurist 1980s electronic energy",
            "groove": drums,
            "percussion": (
                f"{drums}. This track must have its own drum identity; do not default to the same "
                "four-on-the-floor intro or the same snare placement as another track."
            ),
            "bass": (
                f"{bass}. The bass rhythm must function as a unique musical signature for this track, "
                "not a reused one-bar loop from another generation."
            ),
            "melody_density": (
                f"medium. Main hook identity: {lead}. Harmonic identity: {track['harmonic_identity']}. "
                "Avoid generic repeating arpeggio intros unless explicitly requested in the intro identity."
            ),
            "brightness": track["texture_identity"],
            "arrangement": (
                f"Intro identity: {intro}. Arrangement identity: {track['arrangement_identity']}. "
                f"Energy curve: {track['energy_curve']}. The first 30 seconds must be recognizably different "
                "from every other track in this playlist. Do not recycle the same intro, build, breakdown, "
                "or section timing used in another track."
            ),
            "production": (
                "premium wide stereo retro-electronic production with clean headroom, vintage 1980s color, "
                "tight low end, neon synth textures and modern clarity. Preserve the Vice City aesthetic, "
                "but create a completely different composition, groove, hook and arrangement."
            ),
            "title_left": [left],
            "title_right": [right],
        },
        "negative_prompt": [
            "existing song melody",
            "recognizable soundtrack motif",
            "same intro as previous track",
            "same drum loop as previous track",
            "same bass loop as previous track",
            "generic identical arpeggio intro",
            "intelligible vocals",
            "spoken words",
            "rap",
            "hard techno",
            "festival EDM supersaw drop",
            "dubstep",
            "cinematic orchestra",
            "long silence",
            "long ambient drone",
            "clipping",
            "distortion",
            "abrupt cut",
        ],
        "output": {
            "format": "wav",
            "sample_rate": 44100,
            "directory": "/kaggle/working/output",
            "write_manifest": True,
        },
    }

    return {
        "reference_index": index,
        "variant_attempt": variant_attempt,
        "playlist": "GTA VI - Vice City",
        "revision": 2,
        "diversity_contract": queue["diversity_policy"],
        "derived_traits": {
            "title": track["title"],
            "target_bpm": track["target_bpm"],
            "intro_identity": intro,
            "drum_identity": drums,
            "bass_identity": bass,
            "lead_identity": lead,
            "harmonic_identity": track["harmonic_identity"],
            "arrangement_identity": track["arrangement_identity"],
            "texture_identity": track["texture_identity"],
            "energy_curve": track["energy_curve"],
        },
        "reference_copyright_check": {
            "generation_rule": queue["generation_rule"],
            "reference_audio_used": False,
        },
        "profile": profile,
    }

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--index", type=int, required=True)
    ap.add_argument("--variant-attempt", type=int, default=1)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()
    dest = Path(args.out)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(build(args.index, args.variant_attempt), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
