#!/usr/bin/env python3
"""Curated high-energy prompts; no claimed measurement of reference audio."""
import argparse
import json
from pathlib import Path

def build(index):
    queue = json.loads(Path('control/gaming-twitch-dj-30/queue.json').read_text())
    assert 1 <= index <= 30 and len(queue['tracks']) == 30
    track = queue['tracks'][index - 1]
    left, right = track['title'].split(' ', 1)
    profile = {
        'channel': {'name': 'Peter Lofi', 'concept': 'High Energy Gaming — Twitch DJ 30'},
        'generation': {'model': 'medium', 'tracks_per_batch': 1, 'track_duration_seconds': 300,
                       'steps': 8, 'cfg_scale': 1.0, 'chunked_decode': True, 'pure_text_to_audio': True},
        'music_dna': {
            'instrumental_only': True, 'energy': 10,
            'bpm_min': track['target_bpm'], 'bpm_max': track['target_bpm'],
            'style_pool': [track['style']], 'mood': track['mood'].split(', '),
            'preferred_instruments': track['instruments'],
            'listening_context': 'exciting high-energy gaming streams and fast-paced gameplay',
            'groove': 'very energetic driving dance groove, strong rhythmic momentum, lively syncopation',
            'percussion': 'powerful punchy kick and snare, crisp animated hi-hats, exciting fills',
            'bass': 'powerful deep clean synth bass, propulsive rhythmic bassline with sidechain pulse',
            'melody_density': 'catchy original memorable synth motif, bright melodic hooks and energetic arpeggios',
            'brightness': 'brilliant sparkling synths, clean polished highs without harshness',
            'arrangement': 'coherent five-minute composition; energetic entrance, developing original theme, exciting builds and euphoric drops, brief rhythmic contrasts that retain momentum, strong musical ending; high energy throughout',
            'production': 'punchy polished electronic stereo mix, festival energy suitable for gameplay, clean powerful low end, no clipping or dead air',
            'title_left': [left], 'title_right': [right],
        },
        'negative_prompt': ['slow', 'sleepy', 'relaxation music', 'ambient drone', 'lofi sleep beats',
                            'long quiet breakdown', 'long silence', 'clipping', 'distortion', 'abrupt cut',
                            'spoken words', 'intelligible singing', 'existing song melody'],
        'output': {'format': 'wav', 'sample_rate': 44100, 'directory': '/kaggle/working/output', 'write_manifest': True},
    }
    return {'reference_index': index, 'attempt': 1, 'reference': {'playlist': 'twitch-dj-mixed'},
            'feature_source': 'curated_style_from_playlist_metadata',
            'reference_copyright_check': {'generation_rule': 'Original text-to-audio using broad musical style; no reference recording or melody supplied.'},
            'derived_traits': {'title': track['title'], 'target_bpm': track['target_bpm'], 'energy': 'high'},
            'profile': profile}

if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--index', type=int, required=True)
    parser.add_argument('--out', required=True)
    args = parser.parse_args()
    dest = Path(args.out); dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(build(args.index), indent=2) + '\n')
