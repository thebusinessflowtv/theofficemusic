import argparse
import json
import os
import random
from pathlib import Path

import torch
import torchaudio
import yaml
from stable_audio_3 import StableAudioModel

from prompt_engine import build_prompt


def load_profile(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def resolve_runtime(model_name: str) -> tuple[str, bool]:
    """Return (device, model_half) for a supported Stable Audio 3 model."""
    if model_name in {"small-music", "small-sfx"}:
        prefer_cuda = os.getenv("SA3_PREFER_CUDA", "0").strip().lower() in {"1", "true", "yes", "on"}
        if prefer_cuda and torch.cuda.is_available():
            return "cuda", False
        return "cpu", False

    if model_name in {"medium", "medium-base"}:
        if not torch.cuda.is_available():
            raise RuntimeError(
                "Stable Audio 3 Medium requires a CUDA GPU. No CUDA device is available."
            )
        if os.getenv("SA3_ATTENTION_BACKEND") == "sdpa":
            if torch.cuda.get_device_capability(0) < (7, 5):
                raise RuntimeError("The SDPA music runtime requires a T4 or newer CUDA GPU.")
            from stable_audio_3.models import transformer
            # Select the upstream memory-bounded native attention implementation.
            # Avoid Flash Attention 2 and Triton/Flex kernels unsupported by T4.
            transformer.flash_attn_func = None
            transformer.flash_attn_varlen_func = None
            transformer.flex_attention_available = False
            transformer.flex_attention_compiled = None
            print("Attention backend: native SDPA (T4 compatible)", flush=True)
            return "cuda", True
        try:
            import flash_attn  # noqa: F401
        except Exception as exc:
            raise RuntimeError(
                "Stable Audio 3 Medium requires Flash Attention 2, but it is not importable."
            ) from exc

        major, minor = torch.cuda.get_device_capability(0)
        if major < 8:
            raise RuntimeError(
                "Stable Audio 3 Medium requires an Ampere-or-newer GPU; "
                f"received {torch.cuda.get_device_name(0)} with compute capability {major}.{minor}."
            )
        return "cuda", True

    raise ValueError(f"Unsupported Stable Audio 3 model for this pipeline: {model_name}")


def validate_duration(model_name: str, duration: int) -> None:
    if duration < 10:
        raise ValueError("Track duration must be at least 10 seconds.")
    if model_name.startswith("small-") and duration > 120:
        raise ValueError("Stable Audio 3 Small models support at most 120 seconds per generation.")
    if model_name in {"medium", "medium-base"} and duration > 380:
        raise ValueError("Stable Audio 3 Medium supports at most 380 seconds per generation.")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="config/channel_profile.yaml")
    parser.add_argument("--tracks", type=int, default=None)
    parser.add_argument("--duration-seconds", type=int, default=None)
    parser.add_argument("--batch-name", default="office-session")
    parser.add_argument("--master-seed", type=int, default=None)
    args = parser.parse_args()

    profile = load_profile(args.config)
    generation = profile["generation"]
    output_cfg = profile["output"]
    model_name = str(generation["model"])
    duration = int(args.duration_seconds or generation["track_duration_seconds"])

    # Keep prompt metadata synchronized with a runtime duration override.
    generation["track_duration_seconds"] = duration

    if not generation.get("pure_text_to_audio", True):
        raise RuntimeError("This repository is configured for pure text-to-audio generation only.")

    validate_duration(model_name, duration)
    device, model_half = resolve_runtime(model_name)

    output_dir = Path(output_cfg.get("directory", "/kaggle/working/output"))
    output_dir.mkdir(parents=True, exist_ok=True)

    master_seed = (
        args.master_seed
        if args.master_seed is not None
        else random.SystemRandom().randint(1, 2**31 - 1)
    )
    rng = random.Random(master_seed)
    track_count = args.tracks or int(generation["tracks_per_batch"])

    print(f"Loading Stable Audio 3 model: {model_name}")
    print(f"Runtime device: {device}")
    if device == "cuda":
        print(f"GPU: {torch.cuda.get_device_name(0)}")

    model = StableAudioModel.from_pretrained(
        model_name,
        device=device,
        model_half=model_half,
    )
    sample_rate = int(model.model.sample_rate)

    manifest = {
        "batch_name": args.batch_name,
        "master_seed": master_seed,
        "model": model_name,
        "runtime_device": device,
        "sample_rate": sample_rate,
        "pure_text_to_audio": True,
        "track_duration_seconds": duration,
        "tracks": [],
    }

    used_titles = set()
    for i in range(1, track_count + 1):
        prompt, negative_prompt, metadata = build_prompt(profile, i, rng)
        seed = rng.randint(1, 99998)

        title = metadata.get("title") or f"Office Session {i:02d}"
        if title in used_titles:
            title = f"{title} {i:02d}"
        used_titles.add(title)
        metadata["title"] = title

        print(f"Generating track {i}/{track_count} | {title} | seed={seed} | {duration}s")
        print(prompt)

        with torch.inference_mode():
            audio = model.generate(
                prompt=prompt,
                negative_prompt=negative_prompt,
                duration=duration,
                sample_size=int(model.model_config["sample_size"]),
                steps=int(generation.get("steps", 8)),
                cfg_scale=float(generation.get("cfg_scale", 1.0)),
                seed=seed,
                batch_size=1,
                chunked_decode=bool(generation.get("chunked_decode", True)),
            )

        safe_title = "".join(c.lower() if c.isalnum() else "-" for c in title).strip("-")
        safe_title = "-".join(filter(None, safe_title.split("-")))[:64] or f"track-{i:02d}"
        filename = f"{i:02d}-{safe_title}.wav"
        filepath = output_dir / filename
        waveform = audio[0].detach().to(torch.float32).cpu()
        if os.getenv("SA3_ATTENTION_BACKEND") == "sdpa":
            target_samples = sample_rate * duration
            print(f"Generated samples: {waveform.shape[-1]} / {target_samples} requested", flush=True)
            if waveform.shape[-1] < target_samples:
                raise RuntimeError(f"Generated waveform is incomplete: {waveform.shape[-1] / sample_rate:.3f}s / {duration}s requested.")
            if not torch.isfinite(waveform).all():
                raise RuntimeError("Generated waveform contains non-finite samples.")
            # The codec pads its output to decoder blocks. Deliver exactly the
            # requested duration, with a short musical fade and peak headroom
            # before PCM encoding so the generated floats are never clipped.
            waveform = waveform[..., :target_samples].clone()
            fade_in = min(sample_rate // 4, target_samples)
            fade_out = min(sample_rate * 2, target_samples)
            waveform[..., :fade_in] *= torch.linspace(0, 1, fade_in)
            waveform[..., -fade_out:] *= torch.linspace(1, 0, fade_out)
            peak = float(waveform.abs().max())
            if peak > 0.7:
                waveform *= 0.7 / peak
            print(f"Audio finalized: {duration}s, peak={float(waveform.abs().max()):.4f}", flush=True)
        torchaudio.save(str(filepath), waveform, sample_rate)

        manifest["tracks"].append(
            {
                **metadata,
                "filename": filename,
                "seed": seed,
                "prompt": prompt,
                "negative_prompt": negative_prompt,
                "duration_seconds": duration,
            }
        )

        del audio, waveform
        if torch.cuda.is_available():
            torch.cuda.empty_cache()

    manifest_path = output_dir / f"{args.batch_name}_manifest.json"
    with open(manifest_path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)

    print(f"Done. Files saved to {output_dir}")
    print(f"Manifest: {manifest_path}")


if __name__ == "__main__":
    main()
