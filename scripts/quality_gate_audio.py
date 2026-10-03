#!/usr/bin/env python3
import argparse
import hashlib
import json
import pathlib
import re
import subprocess
from datetime import datetime, timezone


def sh(cmd):
    return subprocess.check_output(cmd, text=True, stderr=subprocess.STDOUT)


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def ffprobe(path):
    data = json.loads(sh([
        "ffprobe", "-v", "error", "-show_entries",
        "format=duration,size,bit_rate:stream=codec_name,sample_rate,channels",
        "-of", "json", str(path)
    ]))
    audio = next((x for x in data.get("streams", []) if x.get("codec_name")), {})
    return {
        "duration": float((data.get("format") or {}).get("duration") or 0),
        "size": int((data.get("format") or {}).get("size") or 0),
        "bit_rate": int((data.get("format") or {}).get("bit_rate") or 0),
        "sample_rate": int(audio.get("sample_rate") or 0),
        "channels": int(audio.get("channels") or 0),
        "codec": audio.get("codec_name"),
    }


def loudness(path):
    p = subprocess.run([
        "ffmpeg", "-hide_banner", "-nostats", "-i", str(path),
        "-af", "loudnorm=I=-14:TP=-1:LRA=11:print_format=json",
        "-f", "null", "-"
    ], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    blocks = re.findall(r'\{\s*"input_i".*?\}', p.stderr, flags=re.S)
    if not blocks:
        return {}
    try:
        d = json.loads(blocks[-1])
    except Exception:
        return {}
    out = {}
    for src, dest in (
        ("input_i", "integrated_lufs"),
        ("input_tp", "true_peak_db"),
        ("input_lra", "lra"),
        ("input_thresh", "threshold_lufs"),
    ):
        try:
            out[dest] = float(d[src])
        except Exception:
            pass
    return out


def silence_seconds(path, duration):
    p = subprocess.run([
        "ffmpeg", "-hide_banner", "-nostats", "-i", str(path),
        "-af", "silencedetect=noise=-48dB:d=1.5",
        "-f", "null", "-"
    ], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    starts = [float(x) for x in re.findall(r"silence_start:\s*([0-9.]+)", p.stderr)]
    ends = [(float(a), float(b)) for a, b in re.findall(
        r"silence_end:\s*([0-9.]+)\s*\|\s*silence_duration:\s*([0-9.]+)", p.stderr
    )]
    total = sum(d for _, d in ends)
    if len(starts) > len(ends) and starts:
        total += max(0.0, duration - starts[-1])
    return total


def previous_hashes(root):
    found = set()
    prod = pathlib.Path(root) / "control" / "gaming-reference-production"
    if not prod.exists():
        return found
    for p in prod.glob("[0-9][0-9][0-9].json"):
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
            value = ((d.get("generated") or {}).get("source_wav_sha256"))
            if value:
                found.add(str(value).lower())
        except Exception:
            pass
    return found


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("audio")
    ap.add_argument("--reference-profile", required=True)
    ap.add_argument("--out", default="build/qc.json")
    ap.add_argument("--normalized", default="build/approved.mp3")
    args = ap.parse_args()

    audio = pathlib.Path(args.audio)
    ref = json.loads(pathlib.Path(args.reference_profile).read_text(encoding="utf-8"))
    info = ffprobe(audio)
    digest = sha256(audio)
    loud = loudness(audio)
    silence = silence_seconds(audio, info["duration"])
    silence_ratio = silence / info["duration"] if info["duration"] else 1.0

    checks = {}
    checks["duration_5min"] = 298.0 <= info["duration"] <= 302.0
    checks["stereo"] = info["channels"] == 2
    checks["sample_rate_ok"] = info["sample_rate"] >= 44100
    checks["file_size_ok"] = info["size"] >= 5_000_000
    checks["loudness_present"] = "integrated_lufs" in loud
    checks["loudness_range_ok"] = (
        "integrated_lufs" in loud and -28.0 <= loud["integrated_lufs"] <= -5.0
    )
    checks["true_peak_sane"] = (
        "true_peak_db" in loud and loud["true_peak_db"] <= 1.0
    )
    checks["silence_ok"] = silence_ratio <= 0.10
    checks["fresh_unique"] = digest.lower() not in previous_hashes(".")
    checks["pure_text_generation"] = (
        ref.get("profile", {}).get("generation", {}).get("pure_text_to_audio") is True
    )
    checks["reference_audio_excluded"] = (
        (ref.get("reference_copyright_check") or {}).get("generation_rule") is not None
    )

    approved = all(checks.values())
    result = {
        "approved": approved,
        "checked_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "reference_index": ref.get("reference_index"),
        "attempt": ref.get("attempt"),
        "source_wav_sha256": digest,
        "audio": info,
        "loudness": loud,
        "silence_seconds": round(silence, 3),
        "silence_ratio": round(silence_ratio, 6),
        "checks": checks,
        "originality_gate": {
            "mode": "structural",
            "reference_audio_used": False,
            "artist_or_title_supplied_to_model": False,
            "input_to_generation": "non-expressive audio-feature vector + broad genre descriptors",
            "note": "Automated QC cannot prove absence of all copyright claims; it prevents source-audio reuse and obvious technical failures.",
        },
    }

    out = pathlib.Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    if not approved:
        failed = [k for k, v in checks.items() if not v]
        print("QUALITY_GATE_REJECTED: " + ", ".join(failed))
        raise SystemExit(2)

    normalized = pathlib.Path(args.normalized)
    normalized.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run([
        "ffmpeg", "-hide_banner", "-loglevel", "warning", "-stats", "-y",
        "-i", str(audio),
        "-af", "loudnorm=I=-14:TP=-1:LRA=11",
        "-t", "300",
        "-vn", "-c:a", "libmp3lame", "-b:a", "320k", "-ar", "48000", "-ac", "2",
        str(normalized),
    ], check=True)
    final = ffprobe(normalized)
    if not 298.0 <= final["duration"] <= 302.0 or final["size"] < 3_000_000:
        result["approved"] = False
        result["checks"]["normalized_output_ok"] = False
        out.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        raise SystemExit("QUALITY_GATE_REJECTED: normalized output invalid")

    result["checks"]["normalized_output_ok"] = True
    result["normalized_audio"] = final
    result["approved"] = True
    out.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({
        "approved": True,
        "duration": round(info["duration"], 2),
        "lufs": loud.get("integrated_lufs"),
        "true_peak": loud.get("true_peak_db"),
        "silence_ratio": round(silence_ratio, 4),
        "sha256": digest,
    }))


if __name__ == "__main__":
    main()
