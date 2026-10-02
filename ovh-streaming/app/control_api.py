#!/usr/bin/env python3
import json
import os
import pathlib
import uuid
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

STATE = pathlib.Path("/state")
PLATFORMS = ("kick", "twitch", "youtube-deep-house", "youtube-rainy")


def iso_now():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def read_json(path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def atomic_json(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


class Handler(BaseHTTPRequestHandler):
    def send_json(self, code, payload):
        raw = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        if self.path == "/health":
            payload = {}
            for platform in PLATFORMS:
                payload[platform] = read_json(STATE / platform / "health.json") or {"status": "unknown"}
            return self.send_json(200, payload)

        if self.path.startswith("/now-playing/"):
            platform = self.path.rsplit("/", 1)[-1]
            if platform not in set(PLATFORMS):
                return self.send_json(404, {"error": "unknown_platform"})
            data = read_json(STATE / platform / "now-playing.json")
            return self.send_json(200 if data else 404, data or {"error": "not_ready"})

        return self.send_json(404, {"error": "not_found"})

    def do_POST(self):
        if not self.path.startswith("/command/"):
            return self.send_json(404, {"error": "not_found"})
        platform = self.path.rsplit("/", 1)[-1]
        if platform not in {"kick", "twitch"}:
            return self.send_json(404, {"error": "unknown_platform"})
        try:
            size = int(self.headers.get("Content-Length", "0"))
            body = json.loads(self.rfile.read(size) or b"{}")
        except Exception:
            return self.send_json(400, {"error": "invalid_json"})
        action = str(body.get("action") or "").lower()
        if action not in {"skip", "previous"}:
            return self.send_json(400, {"error": "unsupported_action"})
        payload = {
            "id": str(uuid.uuid4()),
            "platform": platform,
            "action": action,
            "requested_at": iso_now(),
            "source": str(body.get("source") or "control-api"),
            "user": body.get("user"),
        }
        atomic_json(STATE / platform / "command.json", payload)
        return self.send_json(202, payload)

    def log_message(self, fmt, *args):
        pass


def main():
    host = os.environ.get("CONTROL_HOST", "0.0.0.0")
    port = int(os.environ.get("CONTROL_PORT", "8787"))
    ThreadingHTTPServer((host, port), Handler).serve_forever()


if __name__ == "__main__":
    main()
