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
from urllib.parse import parse_qs, urlsplit

from clipper.batch import parse_links
from clipper.media import channel_handle, channel_id, youtube_url
from clipper.publisher import connection_command, token_path
from clipper.storage import ClipperError, clean_stopped_downloads, existing_thumbnail, read_json
from clipper.workflows import (CLOUT_CUTS, DEFAULT_OUTPUT, MOVIE_RECAP, WORKFLOWS,
                               default_caption, default_seconds, ensure_workflow_folders, workflow_output)

ROOT = Path(__file__).resolve().parent


def command(data, output):
    workflow = data.get("workflow", CLOUT_CUTS)
    if workflow not in WORKFLOWS:
        raise ValueError("Choose a supported content mode.")
    mode = data.get("mode")
    source = data.get("source", "").strip()
    count = int(data.get("count", 3))
    seconds = int(data.get("seconds", default_seconds(workflow)))
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
    caption = data.get("caption", default_caption(workflow))
    visibility = data.get("visibility", "local")
    if caption not in ("pop", "classic", "stream-pop") or visibility not in ("local", "private", "unlisted", "public"):
        raise ValueError("Choose a supported caption style and destination.")
    args += ["--workflow", workflow, "--output", str(output), "--clips", str(count),
             "--seconds", str(seconds), "--caption-style", caption]
    if visibility != "local":
        if not token_path(workflow).is_file():
            raise ValueError(f"Connect YouTube for {WORKFLOWS[workflow]} first with {connection_command(workflow)}.")
        args += ["--publish", visibility]
    return [sys.executable, "-u", str(ROOT / "shorts.py"), *args]


class Worker:
    def __init__(self, output):
        self.output = output
        self.lock = Lock()
        self.process = None
        self.status = "Ready"
        self.workflow = None
        self.lines = deque(maxlen=200)

    def start(self, args, workflow):
        with self.lock:
            if self.process is not None:
                raise ValueError("Stop the current job before starting another.")
            proc = subprocess.Popen(args, cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                    stdin=subprocess.DEVNULL, env={**os.environ, "PYTHONIOENCODING": "utf-8"}, encoding="utf-8", errors="replace",
                                    start_new_session=os.name != "nt")
            self.process, self.status = proc, "Running"
            self.workflow = WORKFLOWS[workflow]
            self.lines.clear()
            Thread(target=self.collect, args=(proc, workflow)).start()

    def collect(self, proc, workflow):
        with proc.stdout:
            for line in proc.stdout:
                with self.lock:
                    self.lines.append(line.rstrip()[:3000])
        code = proc.wait()
        with self.lock:
            if self.status == "Stopping":
                try:
                    clean_stopped_downloads(workflow_output(self.output, workflow) / ".state")
                    self.status = "Stopped"
                except (OSError, ClipperError) as error:
                    self.lines.append(f"Could not clean up the stopped job: {error}")
                    self.status = "Needs attention"
            else:
                self.status = "Completed" if code == 0 else "Needs attention"
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
    for workflow in (CLOUT_CUTS, MOVIE_RECAP):
        ensure_workflow_folders(workflow_output(output, workflow))
    worker = Worker(output)
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
            url = urlsplit(self.path)
            path = url.path
            if path == "/api/status":
                workflow = parse_qs(url.query).get("workflow", [CLOUT_CUTS])[0]
                if workflow not in WORKFLOWS:
                    self.reply({"error": "Choose a supported content mode."}, 400)
                    return
                library = workflow_output(output, workflow)
                with worker.lock:
                    result = {"status": worker.status, "active": worker.process is not None,
                              "jobWorkflow": worker.workflow, "lines": list(worker.lines)}
                result["authorizationSaved"] = token_path(workflow).is_file()
                folders = sorted((library / "clips").glob("*/details.json"),
                                 key=lambda p: p.stat().st_mtime, reverse=True)
                result["clips"] = []
                for item in folders[:40]:
                    if not re.fullmatch(r"[a-f0-9]{12}-[a-f0-9]{12}", item.parent.name):
                        continue
                    try:
                        details = read_json(item, {})
                        receipt = read_json(library / ".state" / "uploads" / (details["upload_key"] + ".json"), {})
                        result["clips"].append({"id": item.parent.name, "title": details["title"],
                                                "description": details.get("description", ""), "videoId": receipt.get("video_id")})
                    except (KeyError, OSError, ClipperError):
                        continue
                self.reply(result)
                return
            if path == "/":
                self.reply((ROOT / "web/index.html").read_text(encoding="utf-8").replace("__SESSION__", token).encode(),
                           kind="text/html; charset=utf-8")
                return
            static = {"/app.js": ROOT / "web/app.js", "/style.css": ROOT / "web/style.css",
                      "/example.jpg": ROOT / "examples/thumbnail.jpg"}
            file = static.get(path)
            prefix = f"/{MOVIE_RECAP}/"
            workflow = MOVIE_RECAP if path.startswith(prefix) else CLOUT_CUTS
            clip_path = re.fullmatch(r"(clips/[a-f0-9]{12}-[a-f0-9]{12})/(short.mp4|thumbnail.jpg|captions.srt)",
                                     path.removeprefix(prefix) if workflow == MOVIE_RECAP else path.removeprefix("/"))
            if clip_path:
                library = workflow_output(output, workflow)
                folder = library / clip_path[1]
                filename = clip_path[2]
                file = existing_thumbnail(folder) if filename == "thumbnail.jpg" else folder / filename
                if not file.resolve().is_relative_to(library):
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
                    worker.start(command(data, output), data.get("workflow", CLOUT_CUTS))
                elif self.path == "/api/connect":
                    worker.start([sys.executable, "-u", str(ROOT / "shorts.py"), "connect",
                                  "--workflow", CLOUT_CUTS], CLOUT_CUTS)
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
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    options = parser.parse_args()
    serve(options.port, options.output.expanduser().resolve())
