#!/usr/bin/env python3
"""Build one original GTA VI - Vice City inspired broad-style profile.

The supplied reference recordings are not passed to the model. Only broad,
non-expressive traits from the owner's analysis are used.
"""
import argparse
import json
from pathlib import Path


def build(index: int) -> dict:
    queue = json.loads(Path("control/gta-vi-vice-city/queue.json").read_text(encoding="utf-8"))
    tracks = queue["generated_tracks"]
    if not (1 <= index <= len(tracks)):
        raise ValueError(f"index must be 1..{len(tracks)}")
    track = tracks[index - 1]
    left, right = track["title"].split(" ", 1)

    profile = {
        "channel": {"name": "Peter Lofi", "concept": "GTA VI - Vice City"},
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
            "preferred_instruments": track["instruments"],
            "listening_context": "a humid neon coastal night drive with retro-futurist 1980s electronic energy",
            "groove": (
                "steady driving 4/4 electro groove, consistent pulse from start to finish, "
                "subtle syncopation, danceable but not aggressive, no abrupt tempo changes"
            ),
            "percussion": (
                "tight punchy 1980s-style electronic kick and snare, crisp closed hi-hats, "
                "tasteful gated claps and tom accents, no trap rolls and no hard-techno pounding"
            ),
            "bass": (
                "warm rounded analog synth bass, pulsing and syncopated, strong low-end movement "
                "with clean sidechain space and a confident night-drive feel"
            ),
            "melody_density": (
                "medium, hypnotic original sequenced arpeggios and short memorable synth motifs, "
                "gradual variation, no recognizable borrowed melody"
            ),
            "brightness": "glossy neon highs, warm low mids, smooth analog character, no harsh treble",
            "arrangement": (
                "coherent five-minute composition with an immediate groove, evolving arpeggios, "
                "short restrained transitions, no long ambient intro, no long breakdown, no giant EDM drop, "
                "constant night-drive momentum and a clean musical ending"
            ),
            "production": (
                "premium wide stereo retro-electronic mix, punchy clean drums, controlled sub bass, "
                "lush analog pads and polished modern headroom while retaining an 1980s Miami-night atmosphere"
            ),
            "title_left": [left],
            "title_right": [right],
        },
        "negative_prompt": [
            "existing song melody",
            "recognizable soundtrack motif",
            "intelligible vocals",
            "spoken words",
            "rap",
            "trap percussion",
            "hard techno",
            "festival EDM supersaw drop",
            "dubstep",
            "cinematic orchestra",
            "acoustic folk",
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
        "attempt": 1,
        "playlist": "GTA VI - Vice City",
        "reference_basis": queue["reference_audio_analysis"],
        "reference_copyright_check": {
            "generation_rule": queue["generation_rule"],
            "reference_audio_used": False,
        },
        "derived_traits": {
            "title": track["title"],
            "target_bpm": track["target_bpm"],
            "style": track["style"],
            "mood": track["mood"],
        },
        "profile": profile,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--index", type=int, required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()
    dest = Path(args.out)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(build(args.index), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
