"""Serve the Shorts dashboard and run one worker job at a time."""

import argparse
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import mimetypes
import os
from pathlib import Path
import re
import secrets
import shutil
import signal
import subprocess
import sys
from threading import Lock, Thread
from urllib.parse import urlsplit

from clipper.batch import parse_links
from clipper.media import channel_handle, channel_id, youtube_url
from clipper.publisher import token_path
from clipper.storage import ClipperError, read_json

ROOT = Path(__file__).resolve().parent


def command(data, output):
    mode = data.get("mode")
    source = data.get("source", "").strip()
    count = int(data.get("count", 3))
    seconds = int(data.get("seconds", 35))
    if not 1 <= count <= 20 or not 5 <= seconds <= 180:
        raise ValueError("Choose 1–20 clips and a duration of 5–180 seconds.")
    if mode == "links":
        args = ["links", *map(youtube_url, parse_links(source))]
        if data.get("perVideo"):
            args.append("--per-video")
    elif mode == "follow" and (channel_id(source) or channel_handle(source)):
        args = ["follow", source]
        if data.get("latest"):
            args.append("--include-latest")
    else:
        raise ValueError("Enter a YouTube channel handle, URL, or ID.")
    caption = data.get("caption", "pop")
    visibility = data.get("visibility", "local")
    if caption not in ("pop", "classic") or visibility not in ("local", "private", "unlisted", "public"):
        raise ValueError("Choose a supported caption style and destination.")
    args += ["--output", str(output), "--clips", str(count), "--seconds", str(seconds), "--caption-style", caption]
    if visibility != "local":
        if not token_path().is_file():
            raise ValueError("Connect YouTube first with python shorts.py connect client_secret.json.")
        args += ["--publish", visibility]
    return [sys.executable, "-u", str(ROOT / "shorts.py"), *args]


class Worker:
    def __init__(self):
        self.lock = Lock()
        self.process = None
        self.status = "Ready"
        self.lines = deque(maxlen=200)

    def start(self, args):
        with self.lock:
            if self.process is not None:
                raise ValueError("Stop the current job before starting another.")
            proc = subprocess.Popen(args, cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                    stdin=subprocess.DEVNULL, encoding="utf-8", errors="replace",
                                    start_new_session=os.name != "nt")
            self.process, self.status = proc, "Running"
            self.lines.clear()
            Thread(target=self.collect, args=(proc,), daemon=True).start()

    def collect(self, proc):
        with proc.stdout:
            for line in proc.stdout:
                with self.lock:
                    self.lines.append(line.rstrip()[:3000])
        code = proc.wait()
        with self.lock:
            self.status = "Stopped" if self.status == "Stopping" else "Completed" if code == 0 else "Needs attention"
            self.process = None

    def stop(self):
        with self.lock:
            proc = self.process
            if proc is None or proc.poll() is not None:
                return
            self.status = "Stopping"
            try:
                if os.name == "nt":
                    subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True)
                else:
                    os.killpg(proc.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass


def serve(port, output):
    worker = Worker()
    token = secrets.token_urlsafe(32)

    class Handler(BaseHTTPRequestHandler):
        def reply(self, data, status=200, kind="application/json"):
            body = json.dumps(data).encode() if kind == "application/json" else data
            self.send_response(status)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def local(self):
            if self.headers.get("Host") in {f"127.0.0.1:{server.server_port}", f"localhost:{server.server_port}"}:
                return True
            self.send_error(403)
            return False

        def do_GET(self):
            if not self.local():
                return
            path = urlsplit(self.path).path
            if path == "/api/status":
                with worker.lock:
                    result = {"status": worker.status, "active": worker.process is not None, "lines": list(worker.lines)}
                folders = sorted((output / "clips").glob("*/details.json"), key=lambda p: p.stat().st_mtime, reverse=True)
                result["clips"] = []
                for item in folders[:40]:
                    if not re.fullmatch(r"[a-f0-9]{12}-[a-f0-9]{12}", item.parent.name):
                        continue
                    try:
                        details = read_json(item, {})
                        receipt = read_json(output / ".state" / "uploads" / (details["upload_key"] + ".json"), {})
                        result["clips"].append({"id": item.parent.name, "title": details["title"],
                                                "videoId": receipt.get("video_id")})
                    except (KeyError, OSError, ClipperError):
                        continue
                self.reply(result)
                return
            if path == "/":
                self.reply((ROOT / "web/index.html").read_text().replace("__SESSION__", token).encode(),
                           kind="text/html; charset=utf-8")
                return
            static = {"/app.js": ROOT / "web/app.js", "/style.css": ROOT / "web/style.css",
                      "/example.jpg": ROOT / "examples/thumbnail.jpg"}
            file = static.get(path)
            if re.fullmatch(r"/clips/[a-f0-9]{12}-[a-f0-9]{12}/(short.mp4|thumbnail.jpg|captions.srt)", path):
                file = output / path.lstrip("/")
                if not file.resolve().is_relative_to(output):
                    file = None
            if file is None or not file.is_file():
                self.send_error(404)
                return
            with file.open("rb") as stream:
                self.send_response(200)
                self.send_header("Content-Type", mimetypes.guess_type(file.name)[0] or "application/octet-stream")
                self.send_header("Content-Length", str(file.stat().st_size))
                self.end_headers()
                try:
                    shutil.copyfileobj(stream, self.wfile)
                except (BrokenPipeError, ConnectionResetError):
                    return

        def do_POST(self):
            if not self.local():
                return
            if not secrets.compare_digest(self.headers.get("X-Shorts-Session", ""), token):
                self.send_error(403)
                return
            try:
                if self.path == "/api/start":
                    size = int(self.headers.get("Content-Length", 0))
                    if not 0 < size <= 65536:
                        raise ValueError("The job request is empty or too large.")
                    data = json.loads(self.rfile.read(size))
                    if not isinstance(data, dict):
                        raise ValueError("Expected a job object.")
                    worker.start(command(data, output))
                elif self.path == "/api/stop":
                    worker.stop()
                else:
                    self.send_error(404)
                    return
                self.reply({"ok": True})
            except (TypeError, AttributeError, OSError, ValueError, ClipperError) as error:
                self.reply({"error": str(error)}, 400)

        def log_message(self, *_):
            pass

    with ThreadingHTTPServer(("127.0.0.1", port), Handler) as server:
        print(f"Clout Cliperz is running at http://127.0.0.1:{server.server_port}", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            worker.stop()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--output", type=Path, default=Path("shorts-output"))
    options = parser.parse_args()
    serve(options.port, options.output.expanduser().resolve())
