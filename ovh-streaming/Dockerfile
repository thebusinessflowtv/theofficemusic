FROM python:3.12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY app/ /app/
COPY stations/ /config/
COPY ui-test-assets-b64/ /ui-test-assets-b64/
RUN mkdir -p /ui-test-assets && python - <<'PY'
import base64, pathlib, tarfile
src=pathlib.Path('/ui-test-assets-b64/ui-test-assets.tar.gz.b64')
archive=pathlib.Path('/tmp/ui-test-assets.tar.gz')
archive.write_bytes(base64.b64decode(src.read_text(encoding='ascii')))
with tarfile.open(archive,'r:gz') as tf:
    tf.extractall('/ui-test-assets')
archive.unlink(missing_ok=True)
PY

ENV PYTHONUNBUFFERED=1
CMD ["python", "/app/stream_core.py"]
