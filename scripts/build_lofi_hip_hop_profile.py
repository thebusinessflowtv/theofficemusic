#!/usr/bin/env python3
"""Build a distinct, text-only five-minute Lofi Hip Hop prompt for each indexed track."""
import argparse
import json
from pathlib import Path

QUEUE=Path("control/lofi-hip-hop/queue.json")
RETRY_INTROS=[
    "On retry, start with a bass-only phrase for 4 bars, then an unexpected piano answer before the drums.",
    "On retry, begin with an offbeat drum fill, stop for half a beat, then start guitar and Rhodes together.",
    "On retry, introduce the lead melody rubato without drums and change the chord inversion before the groove.",
    "On retry, use alternating left-right piano and plucked bass accents for the first eight beats."
]
RETRY_GROOVES=[
    "Vary the kick placement and leave more empty space around the snare.",
    "Use extra ghost notes, less busy hats, and a longer two-bar drum phrase.",
    "Accent the unexpected sixteenth while leaving kick-free gaps in the first section.",
    "Use a subtle swung pickup and change the drum groove after the first refrain."
]

def build(index,attempt):
    data=json.loads(QUEUE.read_text(encoding="utf-8"))
    track=next((x for x in data["tracks"] if x["index"]==index),None)
    if not track: raise ValueError(f"Unknown Lofi Hip Hop track index: {index}")
    if attempt<1 or attempt>4: raise ValueError("attempt must be 1..4")
    intro=track["intro_identity"]
    groove=track["groove_identity"]
    if attempt>1:
        intro+=". "+RETRY_INTROS[(index+attempt)%len(RETRY_INTROS)]
        groove+=". "+RETRY_GROOVES[(index+attempt*2)%len(RETRY_GROOVES)]
    left,right=track["title"].split(" ",1)
    profile={
        "channel":{"name":"Peter Lofi","concept":"Lofi Hip Hop — 36 unique five-minute instrumentals"},
        "generation":{
            "model":"medium","tracks_per_batch":1,"track_duration_seconds":300,
            "steps":8,"cfg_scale":1.0,"chunked_decode":True,"pure_text_to_audio":True
        },
        "music_dna":{
            "instrumental_only":True,
            "energy":4,
            "bpm_min":track["target_bpm"],"bpm_max":track["target_bpm"],
            "style_pool":["classic mellow instrumental lofi hip hop with jazzy boom bap drums, lush jazz chords, a warm analog character and relaxed head-nod swing"],
            "mood":["cozy","mellow","jazzy","nostalgic","unhurried","focused"],
            "preferred_instruments":[
                "warm Fender Rhodes electric piano seventh and ninth chords",
                track["lead_identity"],track["bass_identity"],
                "dusty soft boom-bap kick and snare with swung hi-hats",
                track["texture_identity"]
            ],
            "listening_context":"relaxed studying, calm home office, journaling and long background listening",
            "groove":groove,
            "percussion":groove+" Keep drums understated with tasteful humanized swing.",
            "bass":track["bass_identity"]+" Unique rhythmic rests and answer notes, gentle not dominant.",
            "melody_density":"sparse memorable original melody built around "+track["lead_identity"]+
                ". Harmonic identity: "+track["harmonic_identity"]+
                ". Create fresh notes and a new chord progression, not a copied motif.",
            "brightness":"rounded muted highs, lovely warm midrange, low-noise polished stereo with subtle texture",
            "arrangement":"Opening must be distinctive: "+intro+
                ". In the first 30 seconds, do not imitate any other playlist opening. "+
                "The complete five-minute composition develops through verse-like A and B instrumental motifs, "+
                "at least one contrasting bridge and fresh outro. Change phrase lengths, harmonic rhythm and "+
                "instrument entry timing between tracks. Never repeat another track's intro or arrangement.",
            "production":"warm musical analog-inspired text-to-audio production, rich jazzy lofi hip-hop "+
                "and organic human swing, controlled low end, gentle tape color and clean dynamics. "+
                "Maintain the exact Lofi Hip Hop genre, never transform into deep house, trap or dance music.",
            "title_left":[left],"title_right":[right]
        },
        "negative_prompt":[
            "intelligible vocals","rapping","spoken words","recognizable melody","copyrighted sample",
            "reused intro","identical drum loop","hard trap hi hats","EDM","techno","four on the floor house",
            "harsh distorted kick","aggressive bass","cinematic trailer","festival drop","long silence",
            "clipping","abrupt ending"
        ],
        "output":{"format":"wav","sample_rate":44100,"directory":"/kaggle/working/output","write_manifest":True}
    }
    return {
        "reference_index":index,"variant_attempt":attempt,"playlist":data["playlist_name"],
        "diversity_contract":data["diversity_policy"],
        "derived_traits":track,
        "reference_copyright_check":{"generation_rule":data["generation_rule"],"reference_audio_used":False},
        "profile":profile
    }

if __name__=="__main__":
    p=argparse.ArgumentParser()
    p.add_argument("--index",type=int,required=True)
    p.add_argument("--variant-attempt",type=int,default=1)
    p.add_argument("--out",required=True)
    a=p.parse_args()
    dest=Path(a.out);dest.parent.mkdir(parents=True,exist_ok=True)
    dest.write_text(json.dumps(build(a.index,a.variant_attempt),ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
