#!/usr/bin/env python3
import argparse
import hashlib
import json
import os
import pathlib
import subprocess
import urllib.request


def sha256(path: pathlib.Path) -> str:
    h = hashlib.sha256()
    with path.open('rb') as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


def duration(path: pathlib.Path) -> float:
    out = subprocess.check_output([
        'ffprobe', '-v', 'error', '-show_entries', 'format=duration',
        '-of', 'default=nw=1:nk=1', str(path)
    ], text=True).strip()
    return float(out)


def previous_release_digests(repo: str, token: str) -> set[str]:
    if not token:
        return set()
    found: set[str] = set()
    page = 1
    while page <= 10:
        url = f'https://api.github.com/repos/{repo}/releases?per_page=100&page={page}'
        req = urllib.request.Request(url, headers={
            'Authorization': f'Bearer {token}',
            'Accept': 'application/vnd.github+json',
            'X-GitHub-Api-Version': '2022-11-28',
            'User-Agent': 'the-office-music-freshness-guard',
        })
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                releases = json.load(resp)
        except Exception as exc:
            print(f'WARNING: historical digest lookup unavailable: {exc}')
            return found
        if not releases:
            break
        for release in releases:
            for asset in release.get('assets', []):
                name = str(asset.get('name', '')).lower()
                digest = str(asset.get('digest') or '')
                if name.endswith('.wav') and digest.startswith('sha256:'):
                    found.add(digest.split(':', 1)[1].lower())
        if len(releases) < 100:
            break
        page += 1
    return found


def esc_concat(path: pathlib.Path) -> str:
    return str(path.resolve()).replace("'", "'\\''")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument('--generated-dir', default='generated')
    ap.add_argument('--target-seconds', type=int, required=True)
    ap.add_argument('--concat-file', default='build/concat.txt')
    ap.add_argument('--manifest-file', default='build/tracks.json')
    args = ap.parse_args()

    wavs = sorted(pathlib.Path(args.generated_dir).glob('*.wav'))
    if not wavs:
        raise SystemExit('FRESHNESS_GUARD: no newly generated WAV files were found')

    rows = []
    seen: dict[str, pathlib.Path] = {}
    for p in wavs:
        digest = sha256(p)
        if digest in seen:
            raise SystemExit(
                f'FRESHNESS_GUARD: duplicate tracks inside this request: {seen[digest].name} == {p.name}'
            )
        seen[digest] = p
        rows.append((p, duration(p), digest))

    repo = os.environ.get('GITHUB_REPOSITORY', '')
    token = os.environ.get('GH_TOKEN', '')
    old = previous_release_digests(repo, token) if repo else set()
    repeated = [(p.name, d) for p, _, d in rows if d in old]
    if repeated:
        names = ', '.join(name for name, _ in repeated)
        raise SystemExit(
            'FRESHNESS_GUARD: newly generated audio exactly matches a previously saved track: ' + names
        )

    total = sum(sec for _, sec, _ in rows)
    if total + 0.5 < args.target_seconds:
        raise SystemExit(
            f'FRESHNESS_GUARD: fresh unique audio totals only {total:.1f}s, '
            f'but {args.target_seconds}s is required. Refusing to repeat tracks.'
        )

    concat = pathlib.Path(args.concat_file)
    concat.parent.mkdir(parents=True, exist_ok=True)
    with concat.open('w', encoding='utf-8') as f:
        elapsed = 0.0
        for p, sec, _ in rows:
            if elapsed >= args.target_seconds:
                break
            f.write(f"file '{esc_concat(p)}'\n")
            elapsed += sec

    manifest = []
    cursor = 0.0
    for i, (p, sec, digest) in enumerate(rows, 1):
        if cursor >= args.target_seconds:
            break
        manifest.append({
            'title': p.stem,
            'filename': p.name,
            'duration_seconds': round(sec, 2),
            'position': i,
            'start_seconds': round(cursor, 2),
            'sha256': digest,
        })
        cursor += sec
    pathlib.Path(args.manifest_file).write_text(json.dumps(manifest, indent=2), encoding='utf-8')
    print(
        f'FRESHNESS_GUARD_OK: {len(rows)} unique fresh tracks; '
        f'{total:.1f}s available; target={args.target_seconds}s; no repetition permitted.'
    )


if __name__ == '__main__':
    main()
