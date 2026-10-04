FROM python:3.12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY app/ /app/
COPY stations/ /config/
COPY ui-test-assets-b64/ /ui-test-assets-b64/
RUN mkdir -p /ui-test-assets && python - <<'PY'
import base64, pathlib
src=pathlib.Path('/ui-test-assets-b64')
dst=pathlib.Path('/ui-test-assets')
for inp,name in [
    ('background.mp4.b64','background.mp4'),
    ('overlay-static.png.b64','overlay-static.png'),
    ('superstar.ttf.b64','superstar.ttf'),
]:
    (dst/name).write_bytes(base64.b64decode((src/inp).read_text(encoding='ascii')))
PY

ENV PYTHONUNBUFFERED=1
CMD ["python", "/app/stream_core.py"]
