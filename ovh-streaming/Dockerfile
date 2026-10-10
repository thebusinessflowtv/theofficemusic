FROM python:3.12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg ca-certificates fonts-dejavu-core fonts-symbola \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY app/ /app/
COPY stations/ /config/
ENV PYTHONUNBUFFERED=1
CMD ["python", "/app/stream_core.py"]
