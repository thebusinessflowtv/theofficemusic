#!/usr/bin/env python3
import argparse
import json
from pathlib import Path

FAMILY_DETAILS = {
    "inner-light-family": {
        "style": "moody melodic electronic house with organic deep-house warmth and nocturnal atmosphere",
        "mood": ["nocturnal", "melancholic", "hypnotic", "warm", "driving"],
        "instruments": [
            "warm rounded synth bass",
            "restrained four-on-the-floor electronic drums",
            "wide atmospheric polysynth pads",
            "muted pluck or soft mono synth motif",
            "subtle percussive textures",
        ],
        "groove_options": [
            "steady four-on-the-floor pulse with sparse offbeat hats and a restrained clap",
            "deep-house groove with syncopated kick variation and dry rim accents",
            "minimal club pulse with ghost percussion and short tom answers",
            "straight kick foundation with lightly shuffled hats and sparse percussion",
            "warm electronic groove with occasional kick dropouts and subtle syncopation",
        ],
        "lead_options": [
            "short glassy synth motif",
            "soft analog mono lead phrase",
            "muted pluck hook with dotted delay",
            "wide chord-stab hook instead of a lead melody",
            "low-register synth motif answered by high atmospheric notes",
        ],
    },
    "long-road-family": {
        "style": "driving 1980s road-rock and pop-rock instrumental with highway-night energy",
        "mood": ["driving", "restless", "open-road", "confident", "cinematic"],
        "instruments": [
            "crunchy but clean electric rhythm guitar",
            "melodic electric guitar hook",
            "live-feeling punchy rock drums",
            "supportive electric bass",
            "subtle 1980s synth pad",
        ],
        "groove_options": [
            "straight 4/4 rock groove with firm snare and eighth-note hi-hat drive",
            "tom-accented rock beat with kick variations and open-hat lifts",
            "lean pop-rock beat with tight snare, sparse crashes and bass-guitar lock",
            "driving highway beat with kick on strong beats and propulsive hats",
            "slightly syncopated rock-pop groove with floor-tom punctuation",
        ],
        "lead_options": [
            "short melodic electric-guitar riff",
            "clean chorus-guitar hook with sustained notes",
            "palm-muted guitar figure that opens into broad chords",
            "dual-guitar call-and-response motif",
            "guitar hook supported by a subtle vintage synth counterline",
        ],
    },
    "se-me-nota-family": {
        "style": "high-energy Caribbean and Latin urban dance instrumental with bright party momentum",
        "mood": ["festive", "energetic", "tropical", "playful", "danceable"],
        "instruments": [
            "punchy syncopated Latin electronic percussion",
            "deep rhythmic synth bass",
            "bright brass-like synth stabs",
            "short tropical keyboard or pluck motifs",
            "hand percussion and crisp claps",
        ],
        "groove_options": [
            "dembow-informed electronic groove with syncopated kick and snare motion",
            "Caribbean club groove with hand percussion and bright offbeat accents",
            "fast Latin dance pulse with layered conga-style hits and compact kick pattern",
            "minimal urbano groove with heavy bass punctuation and sparse claps",
            "party-driven syncopated groove with rolling percussion and short breaks",
        ],
        "lead_options": [
            "bright brass-style synth stabs",
            "short tropical pluck hook",
            "percussive keyboard call-and-response motif",
            "compact horn-like synth phrase with rhythmic rests",
            "playful mallet-synth hook with bass answers",
        ],
    },
}

VARIANT_MODIFIERS = [
    "use a shorter intro and introduce the primary groove earlier",
    "make the first 30 seconds rhythm-led and delay the main hook",
    "use a contrasting bridge texture and a different final section",
    "invert the density curve: sparse first minute, fuller middle, stripped final groove",
]

def build(index: int, variant_attempt: int) -> dict:
    queue = json.loads(Path("control/gta-vi-extra-15/queue.json").read_text(encoding="utf-8"))
    item = next((x for x in queue["tracks"] if int(x["index"]) == index), None)
    if item is None:
        raise ValueError("Unknown extra-track index")

    family = FAMILY_DETAILS[item["family"]]
    family_tracks = [x for x in queue["tracks"] if x["family"] == item["family"]]
    family_pos = next(i for i, x in enumerate(family_tracks) if x["index"] == index)
    modifier = VARIANT_MODIFIERS[(variant_attempt - 1) % len(VARIANT_MODIFIERS)]

    left, right = item["title"].split(" ", 1)
    groove = family["groove_options"][family_pos]
    lead = family["lead_options"][family_pos]

    profile = {
        "channel": {"name": "Peter Lofi", "concept": "GTA VI - Vice City — reference families"},
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
            "bpm_min": item["target_bpm"],
            "bpm_max": item["target_bpm"],
            "style_pool": [family["style"]],
            "mood": family["mood"],
            "preferred_instruments": family["instruments"],
            "listening_context": "an original five-minute GTA VI / Vice City playlist track for driving, gaming and nightlife listening",
            "groove": groove,
            "percussion": groove + ". Keep the rhythm identity unique within this five-track family.",
            "bass": item["bass_identity"] + ". The bass rhythm must not reuse an existing GTA playlist bass loop.",
            "melody_density": (
                f"medium. Lead identity: {lead}. Harmonic identity: {item['harmonic_identity']}. "
                "Create a new hook from scratch; do not quote or paraphrase the reference song's melody."
            ),
            "brightness": "polished, colorful and modern with controlled highs and a strong but clean low end",
            "arrangement": (
                f"Intro identity: {item['intro_identity']}. Arrangement identity: {item['arrangement_identity']}. "
                f"Variant instruction: {modifier}. The first 45 seconds, drum map, bass rhythm, main hook and section order "
                "must be recognizably different from every approved GTA VI track."
            ),
            "production": (
                "premium stereo mix with strong musical identity, clean headroom and no dead air. "
                "Retain only broad stylistic characteristics from the reference family; never reproduce recognizable "
                "melody, lyrics, riffs, vocal cadence, chord sequence or arrangement from the source track."
            ),
            "title_left": [left],
            "title_right": [right],
        },
        "negative_prompt": [
            "recognizable reference melody",
            "copied guitar riff",
            "copied vocal cadence",
            "copied chord sequence",
            "same intro as another GTA track",
            "same drum loop as another GTA track",
            "same bass loop as another GTA track",
            "intelligible vocals",
            "spoken words",
            "rap",
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
        "playlist": queue["playlist_name"],
        "family": item["family"],
        "reference": {
            "title": item["reference_title"],
            "artist": item["reference_artist"],
            "url": item["reference_url"],
            "usage": "broad musical traits only; source recording not used as model input",
        },
        "derived_traits": {
            "title": item["title"],
            "target_bpm": item["target_bpm"],
            "intro_identity": item["intro_identity"],
            "groove_identity": groove,
            "lead_identity": lead,
            "bass_identity": item["bass_identity"],
            "harmonic_identity": item["harmonic_identity"],
            "arrangement_identity": item["arrangement_identity"],
        },
        "reference_copyright_check": {
            "generation_rule": queue["originality_rule"],
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
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(build(args.index, args.variant_attempt), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
