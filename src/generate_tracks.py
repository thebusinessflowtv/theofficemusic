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


def ensure_environment(model_name: str):
    if not torch.cuda.is_available():
        raise RuntimeError("A CUDA GPU is required in this Kaggle pipeline.")

    # Stable Audio 3 Medium uses SAME-L and requires Flash Attention 2.
    # Small-Music uses SAME-S and can run on older CUDA GPUs such as Kaggle T4.
    if model_name in {"medium", "medium-base"}:
        try:
            import flash_attn  # noqa: F401
        except Exception as exc:
            raise RuntimeError(
                "Flash Attention 2 is required for Stable Audio 3 Medium and is not importable."
            ) from exc
        major, minor = torch.cuda.get_device_capability(0)
        if major < 8:
            raise RuntimeError(
                f"Stable Audio 3 Medium requires an Ampere-or-newer GPU in this pipeline; "
                f"received {torch.cuda.get_device_name(0)} with compute capability {major}.{minor}."
            )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="config/channel_profile.yaml")
    parser.add_argument("--tracks", type=int, default=None)
    parser.add_argument("--batch-name", default="office-session")
    parser.add_argument("--master-seed", type=int, default=None)
    args = parser.parse_args()

    profile = load_profile(args.config)
    generation = profile["generation"]
    output_cfg = profile["output"]
    model_name = generation["model"]

    ensure_environment(model_name)

    if not generation.get("pure_text_to_audio", True):
        raise RuntimeError("This repository is configured for pure text-to-audio generation only.")

    output_dir = Path(output_cfg.get("directory", "/kaggle/working/output"))
    output_dir.mkdir(parents=True, exist_ok=True)

    master_seed = args.master_seed if args.master_seed is not None else random.SystemRandom().randint(1, 2**31 - 1)
    rng = random.Random(master_seed)
    track_count = args.tracks or int(generation["tracks_per_batch"])

    print(f"Loading Stable Audio 3 model: {model_name}")
    print(f"GPU: {torch.cuda.get_device_name(0)}")
    model = StableAudioModel.from_pretrained(model_name, device="cuda")
    sample_rate = int(model.model.sample_rate)

    manifest = {
        "batch_name": args.batch_name,
        "master_seed": master_seed,
        "model": model_name,
        "sample_rate": sample_rate,
        "pure_text_to_audio": True,
        "tracks": [],
    }

    for i in range(1, track_count + 1):
        prompt, negative_prompt, metadata = build_prompt(profile, i, rng)
        seed = rng.randint(1, 99998)
        duration = int(generation["track_duration_seconds"])

        print(f"Generating track {i}/{track_count} | seed={seed} | {duration}s")
        print(prompt)

        with torch.inference_mode():
            audio = model.generate(
                prompt=prompt,
                negative_prompt=negative_prompt,
                duration=duration,
                steps=int(generation.get("steps", 8)),
                cfg_scale=float(generation.get("cfg_scale", 1.0)),
                seed=seed,
                batch_size=1,
                chunked_decode=bool(generation.get("chunked_decode", True)),
            )

        filename = f"{args.batch_name}_track_{i:02d}.wav"
        filepath = output_dir / filename
        waveform = audio[0].detach().to(torch.float32).cpu()
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
        torch.cuda.empty_cache()

    manifest_path = output_dir / f"{args.batch_name}_manifest.json"
    with open(manifest_path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)

    print(f"Done. Files saved to {output_dir}")
    print(f"Manifest: {manifest_path}")


if __name__ == "__main__":
    main()
