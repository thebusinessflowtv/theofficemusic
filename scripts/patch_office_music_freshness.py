#!/usr/bin/env python3
from pathlib import Path

p = Path('.github/workflows/office-music-publish.yml')
s = p.read_text(encoding='utf-8')


def once(old: str, new: str, label: str) -> None:
    global s
    if old not in s:
        raise SystemExit(f'Patch anchor not found: {label}')
    s = s.replace(old, new, 1)


once(
'''      thumbnail_url:
        description: Public thumbnail URL
        required: false
        default: ""
        type: string
''',
'''      thumbnail_url:
        description: Public thumbnail URL
        required: false
        default: ""
        type: string
      visual_mode:
        description: Visual mode: video or image
        required: false
        default: video
        type: string
      visual_image_url:
        description: Public fixed-image URL when visual_mode=image
        required: false
        default: ""
        type: string
''', 'workflow inputs')

once(
'''          INPUT_PUBLISH: ${{ inputs.publish }}
''',
'''          INPUT_PUBLISH: ${{ inputs.publish }}
          VISUAL_MODE: ${{ inputs.visual_mode }}
          VISUAL_IMAGE_URL: ${{ inputs.visual_image_url }}
''', 'validation env')

once(
'''          if os.environ.get('INPUT_PUBLISH', 'true') == 'true':
              for key in ['YOUTUBE_CLIENT_ID','YOUTUBE_CLIENT_SECRET','YOUTUBE_REFRESH_TOKEN','YOUTUBE_CHANNEL_ID']:
                  if not os.environ.get(key):
                      raise SystemExit(f'Missing {key}')
          print(f'job={job} duration={duration}m tracks={tracks} track_seconds={seconds}')
''',
'''          visual_mode = os.environ.get('VISUAL_MODE', 'video').strip().lower() or 'video'
          if visual_mode not in {'video', 'image'}:
              raise SystemExit('visual_mode must be video or image')
          if visual_mode == 'image' and not os.environ.get('VISUAL_IMAGE_URL', '').strip():
              raise SystemExit('visual_image_url is required when visual_mode=image')
          if os.environ.get('INPUT_PUBLISH', 'true') == 'true':
              for key in ['YOUTUBE_CLIENT_ID','YOUTUBE_CLIENT_SECRET','YOUTUBE_REFRESH_TOKEN','YOUTUBE_CHANNEL_ID']:
                  if not os.environ.get(key):
                      raise SystemExit(f'Missing {key}')
          print(f'job={job} duration={duration}m tracks={tracks} track_seconds={seconds} visual={visual_mode}')
''', 'validation logic')

once(
'''        env:
          TRACK_COUNT: ${{ inputs.track_count }}
          TRACK_SECONDS: ${{ inputs.track_seconds }}
        run: |
''',
'''        env:
          TRACK_COUNT: ${{ inputs.track_count }}
          TRACK_SECONDS: ${{ inputs.track_seconds }}
          JOB_ID: ${{ inputs.job_id }}
        run: |
''', 'medium env')

once(
'''          text = re.sub(r'TRACK_DURATION_SECONDS = \\d+', f"TRACK_DURATION_SECONDS = {int(os.environ['TRACK_SECONDS'])}", text)
          p.write_text(text, encoding='utf-8')
''',
'''          text = re.sub(r'TRACK_DURATION_SECONDS = \\d+', f"TRACK_DURATION_SECONDS = {int(os.environ['TRACK_SECONDS'])}", text)
          text = re.sub(r'REQUEST_ID = "[^"]*"', f'REQUEST_ID = "{os.environ["JOB_ID"]}"', text, count=1)
          p.write_text(text, encoding='utf-8')
''', 'medium request id injection')

once(
'''          kaggle kernels push -p "$TMP" --timeout 7200
          KERNEL="$KAGGLE_USERNAME/$KERNEL_SLUG"
''',
'''          kaggle kernels push -p "$TMP" --timeout 7200
          # Prevent the fixed Kaggle slug from being mistaken for a previous completed version.
          sleep 45
          KERNEL="$KAGGLE_USERNAME/$KERNEL_SLUG"
''', 'medium stale-version delay')

once(
'''          kaggle kernels output "$KERNEL" -p generated-medium
          test -n "$(find generated-medium -type f -name '*.wav' -print -quit)"
''',
'''          kaggle kernels output "$KERNEL" -p generated-medium
          test -n "$(find generated-medium -type f -name '*.wav' -print -quit)"
          MARKER="$(find generated-medium -type f -name 'request_id.txt' -print -quit)"
          test -n "$MARKER"
          test "$(tr -d '\\r\\n ' < "$MARKER")" = "$JOB_ID" || { echo "::error::Stale Kaggle Medium output rejected"; exit 1; }
''', 'medium request marker')

once(
'''        env:
          DURATION_MINUTES: ${{ inputs.duration_minutes }}
        run: |
          set -euo pipefail
          TMP="$(mktemp -d)"
          cp kaggle/runner_small.py "$TMP/runner.py"
''',
'''        env:
          DURATION_MINUTES: ${{ inputs.duration_minutes }}
          JOB_ID: ${{ inputs.job_id }}
        run: |
          set -euo pipefail
          TMP="$(mktemp -d)"
          cp kaggle/runner_small.py "$TMP/runner.py"
''', 'small env')

once(
'''          text = re.sub(r'TRACK_COUNT = \\d+', f"TRACK_COUNT = {int(os.environ['SMALL_COUNT'])}", text)
          p.write_text(text, encoding='utf-8')
''',
'''          text = re.sub(r'TRACK_COUNT = \\d+', f"TRACK_COUNT = {int(os.environ['SMALL_COUNT'])}", text)
          text = re.sub(r'REQUEST_ID = "[^"]*"', f'REQUEST_ID = "{os.environ["JOB_ID"]}"', text, count=1)
          p.write_text(text, encoding='utf-8')
''', 'small request id injection')

once(
'''          kaggle kernels push -p "$TMP" --timeout 7200
          KERNEL="$KAGGLE_USERNAME/$SMALL_KERNEL_SLUG"
''',
'''          kaggle kernels push -p "$TMP" --timeout 7200
          sleep 45
          KERNEL="$KAGGLE_USERNAME/$SMALL_KERNEL_SLUG"
''', 'small stale-version delay')

once(
'''          kaggle kernels output "$KERNEL" -p generated-small
          test -n "$(find generated-small -type f -name '*.wav' -print -quit)"
''',
'''          kaggle kernels output "$KERNEL" -p generated-small
          test -n "$(find generated-small -type f -name '*.wav' -print -quit)"
          MARKER="$(find generated-small -type f -name 'request_id.txt' -print -quit)"
          test -n "$MARKER"
          test "$(tr -d '\\r\\n ' < "$MARKER")" = "$JOB_ID" || { echo "::error::Stale Kaggle Small output rejected"; exit 1; }
''', 'small request marker')

once(
'''          else
            echo "::warning::Kaggle generation unavailable. Using approved generated calibration master as a last-resort continuity fallback."
            curl --retry 5 --retry-all-errors -fL \\
              -H "Authorization: Bearer $GH_TOKEN" \\
              -H "Accept: application/vnd.github+json" \\
              -H "X-GitHub-Api-Version: 2022-11-28" \\
              "https://api.github.com/repos/thebusinessflowtv/theofficemusic/actions/artifacts/11009873245/zip" \\
              -o build/fallback.zip
            unzip -o build/fallback.zip -d build/fallback
            find build/fallback -type f -name '*.wav' -exec cp {} generated/ \;
            echo approved-calibration-master-fallback > build/generation-mode.txt
          fi
''',
'''          else
            echo "::error::Fresh generation failed in both Medium and Small-Music. Historical audio reuse is forbidden."
            exit 1
          fi
''', 'historical audio fallback')

start = s.index('      - name: Build exact-duration mix\n')
end = s.index('      - name: Prepare resilient visual and thumbnail\n', start)
s = s[:start] + '''      - name: Build exact-duration mix with freshness guard
        env:
          DURATION_MINUTES: ${{ inputs.duration_minutes }}
          GH_TOKEN: ${{ github.token }}
        run: |
          set -euo pipefail
          TARGET_SECONDS=$((DURATION_MINUTES * 60))
          python scripts/validate_fresh_audio.py \\
            --generated-dir generated \\
            --target-seconds "$TARGET_SECONDS" \\
            --concat-file build/concat.txt \\
            --manifest-file build/tracks.json
          ffmpeg -hide_banner -loglevel warning -stats -y \\
            -f concat -safe 0 -i build/concat.txt \\
            -t "$TARGET_SECONDS" -vn \\
            -c:a aac -b:a 320k -ar 48000 build/mix.m4a
          test -s build/mix.m4a

''' + s[end:]

start = s.index('      - name: Prepare resilient visual and thumbnail\n')
end = s.index('      - name: Encode short 4K visual master once\n', start)
s = s[:start] + '''      - name: Prepare selected visual and thumbnail
        env:
          LOOP_URL: ${{ inputs.loop_url }}
          THUMBNAIL_URL: ${{ inputs.thumbnail_url }}
          VISUAL_MODE: ${{ inputs.visual_mode }}
          VISUAL_IMAGE_URL: ${{ inputs.visual_image_url }}
        run: |
          set -euo pipefail
          mkdir -p build
          if [ -n "$THUMBNAIL_URL" ]; then
            curl --retry 5 --retry-all-errors -fL "$THUMBNAIL_URL" -o build/thumbnail.jpg || true
          fi
          if ! ffmpeg -hide_banner -v error -i build/thumbnail.jpg -frames:v 1 -f null - >/dev/null 2>&1; then
            tr -d '\\r\\n ' < assets/bootstrap/thumb_compact.b64 | base64 --decode > build/thumbnail.jpg
          fi
          ffmpeg -hide_banner -v error -i build/thumbnail.jpg -frames:v 1 -f null - >/dev/null

          if [ "$VISUAL_MODE" = "image" ]; then
            test -n "$VISUAL_IMAGE_URL" || { echo "::error::Fixed-image mode requires visual_image_url"; exit 1; }
            curl --retry 5 --retry-all-errors -fL "$VISUAL_IMAGE_URL" -o build/fixed-visual.img
            ffmpeg -hide_banner -v error -i build/fixed-visual.img -frames:v 1 -f null - >/dev/null 2>&1 || { echo "::error::Selected fixed image is invalid"; exit 1; }
            ffmpeg -hide_banner -loglevel warning -y \\
              -loop 1 -i build/fixed-visual.img -t 12 \\
              -vf "scale=1920:1080:force_original_aspect_ratio=decrease:flags=lanczos,pad=1920:1080:(ow-iw)/2:(oh-ih)/2,setsar=1,fps=30,format=yuv420p" \\
              -an -c:v libx264 -preset veryfast -crf 18 -movflags +faststart build/loop-source.mp4
            echo image > build/visual-mode.txt
          else
            test -n "$LOOP_URL" || { echo "::error::Video mode requires the GitHub master loop URL"; exit 1; }
            curl --retry 5 --retry-all-errors -fL "$LOOP_URL" -o build/loop-source.mp4
            ffmpeg -hide_banner -v error -i build/loop-source.mp4 -map 0:v:0 -f null - >/dev/null 2>&1 || { echo "::error::GitHub master loop is invalid"; exit 1; }
            echo video > build/visual-mode.txt
          fi
          ffprobe -v error -show_entries stream=codec_name,width,height,r_frame_rate -show_entries format=duration,size -of json build/loop-source.mp4

''' + s[end:]

p.write_text(s, encoding='utf-8')
print('Production workflow patched successfully.')
