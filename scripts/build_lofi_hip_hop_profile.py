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

    if index >= 37:
        # Volume 2: upbeat instrumental hip-hop, NEVER sax/jazz-lounge.
        # The original 36 QC-approved track profiles remain byte-for-byte equivalent.
        dna=profile["music_dna"]
        dna.update({
            "energy":track.get("energy_level",7),
            "bpm_min":track["target_bpm"],
            "bpm_max":track["target_bpm"],
            "style_pool":[
                "upbeat original lofi hip hop radio, punchy laid-back boom bap and warm urban head-nod grooves, sample-free",
                "groovy instrumental hip-hop with crisp kicks, confident snare, rolling rhythmic bass and short synth/guitar chops",
                "energetic but relaxed golden-hour beats, tight breakbeat-inspired boom bap, catchy minimalist melodies"
            ],
            "mood":["upbeat","groovy","head-nod","warm","confident","relaxed-focus"],
            "preferred_instruments":[
                "punchy dry kick, sharp rim and snare, lively humanized hi-hat swing",
                track["lead_identity"],
                track["bass_identity"],
                "short rhythmic electric guitar chops or synth stabs (original notes)",
                track["texture_identity"],
            ],
            "listening_context":"energetic hip hop listening for work, drawing, gaming, late-night studying, city walks",
            "groove":groove+"; strong head-nod pocket without becoming trap or club music",
            "percussion":groove+"; upfront punchy kick and snare, lively rolling hats, creative original break variations",
            "bass":track["bass_identity"]+"; bass is active and punchy, always locked to drums",
            "melody_density":"short original synth, guitar, mallet or organ hooks around "+
                track["lead_identity"]+"; simple rhythmic motifs, not jazz improvisation. "+
                "Harmonic color: "+track["harmonic_identity"],
            "brightness":"clear punchy drum transients, warm bass, bright but never piercing hooks",
            "arrangement":"Distinct first 45 seconds: "+intro+
                ". Make a complete ORIGINAL 300-second instrumental: punchy intro, "+
                "A/B grooves, original contrasting bridge at ~2 minutes, fresh melodic variation, "+
                "return of hook, non-abrupt outro. Vary drum placement, fills and melodies. "+
                "Never loop the same eight bars for five minutes.",
            "production":"polished sample-free lofi hip hop, organic swung breakbeat and thick controlled kick/snare, "+
                "warm bass and tight short synth or guitar phrases. Medium-to-high head-nod energy, "+
                "not soft jazzy lounge. No saxophone, no brass solos, no acoustic jazz, "+
                "no piano-dominated ballad, no recognizable Lofi Girl songs or melodies. "+
                "Remain original Peter Lofi instrumental hip hop, NOT trap, house or EDM."
        })
        profile["channel"]["concept"]="Lofi Hip Hop — 30 upbeat new original instrumentals (tracks 37-66)"
        profile["negative_prompt"] += [
            "saxophone","jazz sax","trumpet solo","horn section","jazz lounge",
            "slow sleepy jazz","brushed jazz swing","lengthy Rhodes improvisation",
            "smooth-jazz chords","lofi girl track replication","recognizable third-party melody",
            "sampled recording","trap rolls","four-on-the-floor club drums",
            "non-hip-hop percussion","soft acoustic ambient","washed out drums",
        ]
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
