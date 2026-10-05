#!/usr/bin/env python3
import argparse
import json
import subprocess
import tempfile
from pathlib import Path
from urllib.request import urlretrieve

import numpy as np


def pcm(path, seconds=45):
    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-i", str(path),
        "-t", str(seconds), "-ac", "1", "-ar", "8000", "-f", "f32le", "-"
    ]
    raw = subprocess.check_output(cmd)
    x = np.frombuffer(raw, dtype=np.float32)
    if len(x) == 0:
        raise RuntimeError(f"No PCM decoded from {path}")
    x = x - np.mean(x)
    peak = np.max(np.abs(x))
    if peak > 0:
        x = x / peak
    return x


def frame_features(x, sr=8000, frame=2048, hop=1024, bands=24):
    if len(x) < frame:
        x = np.pad(x, (0, frame-len(x)))
    win = np.hanning(frame).astype(np.float32)
    rows = []
    rms = []
    for start in range(0, max(1, len(x)-frame+1), hop):
        y = x[start:start+frame]
        if len(y) < frame:
            y = np.pad(y, (0, frame-len(y)))
        rms.append(float(np.sqrt(np.mean(y*y)+1e-12)))
        spec = np.abs(np.fft.rfft(y*win)) + 1e-8
        edges = np.geomspace(1, len(spec), bands+1).astype(int)
        feat=[]
        for a,b in zip(edges[:-1],edges[1:]):
            b=max(b,a+1)
            feat.append(float(np.log1p(np.mean(spec[a:b]))))
        v=np.asarray(feat,dtype=np.float32)
        n=np.linalg.norm(v)
        rows.append(v/(n+1e-8))
    return np.vstack(rows), np.asarray(rms,dtype=np.float32)


def aligned_cosine(a,b):
    n=min(len(a),len(b))
    if n == 0:
        return 0.0
    return float(np.mean(np.sum(a[:n]*b[:n],axis=1)))


def corr(a,b):
    n=min(len(a),len(b))
    if n < 4:
        return 0.0
    a=a[:n]; b=b[:n]
    if np.std(a)<1e-7 or np.std(b)<1e-7:
        return 0.0
    return float(np.corrcoef(a,b)[0,1])


def rhythm_signature(rms):
    d=np.maximum(0,np.diff(rms,prepend=rms[:1]))
    if np.max(d)>0:
        d=d/np.max(d)
    ac=np.correlate(d,d,mode="full")[len(d)-1:]
    ac=ac[:min(80,len(ac))]
    if len(ac) and ac[0]>0:
        ac=ac/ac[0]
    return ac.astype(np.float32)


def compare(a_path,b_path):
    a=pcm(a_path); b=pcm(b_path)
    af,ar=frame_features(a); bf,br=frame_features(b)
    spectral=aligned_cosine(af,bf)
    envelope=corr(ar,br)
    rhythm=corr(rhythm_signature(ar),rhythm_signature(br))
    return spectral,envelope,rhythm


def main():
    ap=argparse.ArgumentParser()
    ap.add_argument("audio")
    ap.add_argument("--library", default="control/music-library.json")
    ap.add_argument("--playlist-key", default="gta-vi-vice-city")
    ap.add_argument("--out", default="build/diversity.json")
    args=ap.parse_args()

    library=json.loads(Path(args.library).read_text(encoding="utf-8"))
    playlist=next((p for p in library.get("playlists",[]) if p.get("key")==args.playlist_key),None)
    previous=(playlist or {}).get("tracks",[])
    comparisons=[]
    rejected=False

    with tempfile.TemporaryDirectory() as td:
        for i,t in enumerate(previous):
            url=t.get("url")
            if not url:
                continue
            dest=Path(td)/f"ref-{i:03d}.mp3"
            try:
                urlretrieve(url,dest)
                spectral,envelope,rhythm=compare(args.audio,dest)
                too_close = (
                    (spectral >= 0.985 and envelope >= 0.92)
                    or (spectral >= 0.975 and rhythm >= 0.965)
                    or (envelope >= 0.975 and rhythm >= 0.975)
                )
                comparisons.append({
                    "id":t.get("id"),
                    "title":t.get("title"),
                    "intro_spectral_similarity":round(spectral,6),
                    "intro_envelope_correlation":round(envelope,6),
                    "rhythm_signature_correlation":round(rhythm,6),
                    "too_close":too_close,
                })
                rejected = rejected or too_close
            except Exception as exc:
                comparisons.append({"id":t.get("id"),"title":t.get("title"),"comparison_error":type(exc).__name__})

    result={
        "approved":not rejected,
        "rule":"Reject if first-45s spectral/envelope/rhythm signatures are excessively close to any already-approved playlist track.",
        "compared_against":len(comparisons),
        "comparisons":comparisons,
    }
    Path(args.out).parent.mkdir(parents=True,exist_ok=True)
    Path(args.out).write_text(json.dumps(result,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    print(json.dumps({"approved":not rejected,"compared_against":len(comparisons)}))
    if rejected:
        raise SystemExit(3)


if __name__=="__main__":
    main()
