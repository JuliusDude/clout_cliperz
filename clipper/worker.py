import json
import sys
import time
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from . import publisher
from .workflows import CLOUT_CUTS, DYNAMIC_MODES
from .captions import highlights, transcript
from .media import (EXTENSIONS, VIDEO_ID, channel_handle, channel_id, download, make_clip, probe,
                    resolve_handle, video_id, youtube_url)
from .storage import (ClipperError, cached_hash, clear_download_workspace, digest, existing_thumbnail,
                      read_json, save_json, signature, temporary_downloads, work_lock)


@dataclass
class Source:
    path: Path
    title: str
    origin: str | None
    media_id: str
    duration: int
    has_audio: bool
    words: list
    caption_path: Path
    word_highlighting: bool
    snapshot: tuple

    def check_unchanged(self):
        current = (signature(self.path), signature(self.caption_path) if self.caption_path.is_file() else None)
        if current != self.snapshot:
            raise ClipperError("The video or captions changed during processing. Retry when the copy finishes.")


def prepare_source(source, args, state):
    origin = None
    if urlsplit(str(source)).scheme in {"http", "https"}:
        ident = video_id(str(source))
        path, title = download(ident, state)
        origin = youtube_url(ident)
    else:
        path = Path(source).expanduser().resolve()
        if not path.is_file():
            raise ClipperError(f"Video file does not exist: {path}")
        title = path.stem
    supplied = getattr(args, "srt", None)
    candidate = supplied.expanduser().resolve() if supplied else path.with_suffix(".srt")
    if supplied and not candidate.is_file():
        raise ClipperError(f"Caption file does not exist: {candidate}")
    sidecar = candidate if candidate.is_file() else None
    duration, has_audio, audio_offset = probe(path)
    if not has_audio and sidecar is None:
        raise ClipperError("This video has no audio. Add a matching .srt file with timed captions.")
    before = (signature(path), signature(sidecar) if sidecar else None)
    media_id = cached_hash(path, state)
    words = transcript(path, sidecar, media_id, state, args.model, args.language, duration, audio_offset)
    prepared = Source(path, title, origin, media_id, duration, has_audio, words, candidate, sidecar is None, before)
    prepared.check_unchanged()
    return prepared


def finish_clips(source, clips, args, client, state):
    rendered = completed = 0
    for clip in clips:
        source.check_unchanged()
        try:
            folder, details = make_clip(source.path, clip, source.media_id, source.title, source.origin,
                                        source.has_audio, args.layout, source.word_highlighting,
                                        args.output, args.caption_style)
            source.check_unchanged()
            rendered += 1
            print(f"Captioned Short: {folder / 'short.mp4'}\nThumbnail: {existing_thumbnail(folder)}", flush=True)
            if client is not None:
                publisher.publish_with_thumbnail(client, folder, details, state, args.publish, args.audience)
            completed += 1
        except Exception as error:
            print(f"Could not finish {source.title} at {clip.start / 1000:.1f}s: {error}", file=sys.stderr, flush=True)
    return rendered, completed


def run_source(source, args, client, state):
    with temporary_downloads(state):
        video = prepare_source(source, args, state)
        chosen = highlights(video.words, video.duration, args.seconds, args.clips, args.workflow)
        if len(chosen) < args.clips:
            print(f"Found {len(chosen)} of {args.clips} requested nonoverlapping clips in {video.title}.", flush=True)
        _, completed = finish_clips(video, chosen, args, client, state)
        if completed != len(chosen):
            raise ClipperError(f"Completed {completed} of {len(chosen)} clips. Rerun to finish the pending work.")


def try_source(source, args, client, state):
    try:
        run_source(source, args, client, state)
        return True
    except Exception as error:
        print(f"Could not process {source}: {error}", file=sys.stderr, flush=True)
        return False


def input_signature(path):
    srt = path.with_suffix(".srt")
    return [signature(path), signature(srt) if srt.is_file() else None]


def scan_folder(inbox, args, client, state):
    inbox.mkdir(parents=True, exist_ok=True)
    config = [str(inbox), args.model, args.language, args.seconds, args.clips,
              args.layout, args.publish, args.audience, args.caption_style]
    if args.workflow in DYNAMIC_MODES:
        config.append(args.workflow)
    marker = state / "folders" / (digest(json.dumps(config)) + ".json")
    seen = read_json(marker, {})
    files = sorted(p for p in inbox.iterdir() if p.is_file() and not p.name.startswith(".")
                   and p.suffix.lower() in EXTENSIONS)
    candidates = {}
    for path in files:
        try:
            current = input_signature(path)
            if seen.get(path.name) != current:
                candidates[path] = current
        except FileNotFoundError:
            continue
    if candidates:
        time.sleep(2)  # Sample the entire batch together before opening new files.
    ok = True
    for path, snapshot in candidates.items():
        try:
            if input_signature(path) != snapshot:
                continue
            if try_source(path, args, client, state):
                seen[path.name] = snapshot
                save_json(marker, seen)
            else:
                ok = False
        except FileNotFoundError:
            continue
    return ok


def channel_feed(channel):
    url = "https://www.youtube.com/feeds/videos.xml?channel_id=" + channel
    request = urllib.request.Request(url, headers={"User-Agent": "ShortsClipper/1.0"})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            root = ET.fromstring(response.read(2 * 1024 * 1024))
    except (OSError, ValueError, ET.ParseError) as error:
        raise ClipperError(f"Could not read the public YouTube channel feed for {channel}: {error}") from error
    atom = "{http://www.w3.org/2005/Atom}"
    if root.tag != atom + "feed":
        raise ClipperError("YouTube returned an unexpected feed document.")
    ids = [entry.findtext("{http://www.youtube.com/xml/schemas/2015}videoId", "")
           for entry in root.findall(atom + "entry")]
    return [ident for ident in dict.fromkeys(ids) if VIDEO_ID.fullmatch(ident)]


def scan_channel(channel, args, client, state):
    workflow = getattr(args, "workflow", CLOUT_CUTS)
    prefix = workflow + "-" if workflow in DYNAMIC_MODES else ""
    marker = state / "channels" / (prefix + channel + ".json")
    queue = read_json(marker)
    ok = True
    try:
        ids = channel_feed(channel)  # Newest first.
    except ClipperError as error:
        if queue is None:
            raise
        print(error, file=sys.stderr, flush=True)
        ids, ok = [], False
    if queue is None:
        queue = {"seen": ids, "pending": ids[:1] if args.include_latest else []}
        print(f"Channel registered. Watching for new uploads ({len(ids)} existing entries).", flush=True)
    else:
        fresh = [v for v in reversed(ids) if v not in queue["seen"] and v not in queue["pending"]]
        queue["pending"].extend(fresh)
        # Retain more history than the public feed window.
        queue["seen"] = list(dict.fromkeys(ids + queue["seen"]))[:2000]
    save_json(marker, queue)
    if queue["pending"]:
        posted = {read_json(p).get("video_id") for p in (state / "uploads").glob("*.json")}
        for ident in list(queue["pending"]):
            if ident in posted or try_source(youtube_url(ident), args, client, state):
                queue["pending"].remove(ident)
                save_json(marker, queue)
            else:
                ok = False
    return ok


def watch(args, client, state):
    channel = channel_id(args.source)
    handle = channel_handle(args.source)
    if not (channel or handle) and "://" in args.source:
        raise ClipperError("For channel watching, use a YouTube channel URL, @handle or UC… channel ID.")
    if getattr(args, "command", None) == "follow" and not (channel or handle):
        raise ClipperError("Follow needs a YouTube channel URL, @handle, or UC… channel ID.")
    if args.include_latest and not (channel or handle):
        raise ClipperError("--include-latest applies to a YouTube channel feed.")
    inbox = Path(args.source).expanduser().resolve() if not (channel or handle) else None
    if inbox and inbox.exists() and not inbox.is_dir():
        raise ClipperError("watch needs a folder or a channel ID. Use run for a single video.")
    action = f"posting {args.publish} Shorts" if client is not None else "saving captioned Shorts"
    print(f"Watching {channel or handle or inbox} and {action}. Output: {args.output}", flush=True)
    while True:
        try:
            with work_lock(state):
                clear_download_workspace(state)
                if handle and not channel:
                    channel = resolve_handle(handle, state)
                ok = scan_channel(channel, args, client, state) if channel else scan_folder(inbox, args, client, state)
        except (ClipperError, OSError) as error:
            print(error, file=sys.stderr, flush=True)
            ok = False
        if args.once:
            return 0 if ok else 1
        time.sleep(args.interval)
