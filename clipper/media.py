import json
import math
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlsplit

from .captions import phrase, write_captions
from .cover import write_cover_script
from .storage import ClipperError, digest, read_json, save_json


VIDEO_ID = re.compile(r"[A-Za-z0-9_-]{11}")
CHANNEL_ID = re.compile(r"UC[A-Za-z0-9_-]{22}")
YOUTUBE_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com"}
EXTENSIONS = {".mp4", ".mov", ".mkv", ".webm", ".m4v", ".avi", ".mts", ".m2ts"}


def video_id(url):
    parsed = urlsplit(url)
    parts = parsed.path.strip("/").split("/")
    ident = ""
    if parsed.hostname in {"youtu.be", "www.youtu.be"}:
        ident = parts[0]
    elif parsed.hostname in YOUTUBE_HOSTS:
        if parts == ["watch"]:
            ident = parse_qs(parsed.query).get("v", [""])[0]
        elif len(parts) == 2 and parts[0] in {"shorts", "live", "embed"}:
            ident = parts[1]
    if parsed.scheme not in {"http", "https"} or not VIDEO_ID.fullmatch(ident):
        raise ClipperError("Use a single YouTube video URL or a local video file.")
    return ident


def channel_id(value):
    if CHANNEL_ID.fullmatch(value):
        return value
    parsed = urlsplit(value)
    if parsed.scheme in {"http", "https"} and parsed.hostname in YOUTUBE_HOSTS:
        parts = parsed.path.strip("/").split("/")
        if len(parts) >= 2 and parts[0] == "channel" and CHANNEL_ID.fullmatch(parts[1]):
            return parts[1]
    return None


def channel_handle(value):
    parsed = urlsplit(value)
    if parsed.scheme in {"http", "https"} and parsed.hostname in YOUTUBE_HOSTS:
        value = unquote(parsed.path.strip("/").split("/")[0])
    if value.startswith("@") and len(value) > 1 and not re.search(r"[\s/#?\\\x00-\x1f]", value):
        return value
    return None


def resolve_handle(handle, state):
    record = state / "handles" / (digest(handle.casefold()) + ".json")
    saved = read_json(record)
    if saved:
        return saved["channel_id"]
    try:
        import yt_dlp
    except ImportError as error:
        raise ClipperError("Install requirements.txt to look up YouTube handles, or use a UC channel ID.") from error
    options = {"quiet": True, "extract_flat": "in_playlist", "playlistend": 1, "socket_timeout": 30}
    url = "https://www.youtube.com/" + quote(handle, safe="@._-") + "/videos"
    try:
        with yt_dlp.YoutubeDL(options) as downloader:
            info = downloader.extract_info(url, download=False) or {}
    except yt_dlp.utils.DownloadError as error:
        raise ClipperError(f"Could not resolve {handle}. A UC channel ID can also be used.") from error
    for ident in (info.get("channel_id"), info.get("id")):
        if ident and CHANNEL_ID.fullmatch(ident):
            save_json(record, {"channel_id": ident})
            return ident
    raise ClipperError(f"No channel ID found for {handle}. Try its UC channel ID.")


def youtube_url(ident):
    return "https://www.youtube.com/watch?v=" + ident


def download(ident, state):
    cache = state / "sources" / ident
    source = cache / "source.mkv"
    if source.is_file():
        return source, read_json(cache / "info.json")["title"]
    try:
        import yt_dlp
    except ImportError as error:
        raise ClipperError("Install requirements.txt to import YouTube videos.") from error
    cache.parent.mkdir(parents=True, exist_ok=True)
    print(f"Importing YouTube video {ident} …", flush=True)
    with tempfile.TemporaryDirectory(dir=cache.parent) as tmp:
        stage = Path(tmp) / "download"
        stage.mkdir()
        options = {
            "outtmpl": str(stage / "source.%(ext)s"), "noplaylist": True,
            "format": "bv*[height<=1080]+ba/b[height<=1080]/best",
            "merge_output_format": "mkv", "quiet": True, "noprogress": True,
            "postprocessors": [{"key": "FFmpegVideoRemuxer", "preferedformat": "mkv"}],
            "socket_timeout": 30, "retries": 3,
        }
        def available_replay(info, *, incomplete=False):
            if info.get("live_status") in {"is_live", "is_upcoming", "post_live"}:
                raise ClipperError(f"YouTube video {ident} is waiting for a finished replay.")

        options["match_filter"] = available_replay
        if shutil.which("node"):
            options["js_runtimes"] = {"deno": {}, "node": {}}
        try:
            with yt_dlp.YoutubeDL(options) as downloader:
                info = downloader.extract_info(youtube_url(ident), download=True)
        except yt_dlp.utils.DownloadError as error:
            raise ClipperError(f"YouTube import failed for {ident}. Check source access or use the original file. {error}") from error
        if not (stage / "source.mkv").is_file():
            raise ClipperError("The YouTube importer did not produce a video file.")
        title = info.get("title") or ident
        save_json(stage / "info.json", {"title": title})
        os.replace(stage, cache)
    return source, title


def check_tools():
    for name in ("ffmpeg", "ffprobe"):
        if not shutil.which(name):
            raise ClipperError(f"Install FFmpeg with libass and add {name} to PATH.")


def execute(args, cwd=None):
    result = subprocess.run(args, cwd=cwd, capture_output=True, text=True, errors="replace")
    if result.returncode:
        raise ClipperError(f"{args[0]} failed:\n" + result.stderr.strip()[-1800:])
    return result.stdout


def probe(path):
    raw = execute(["ffprobe", "-v", "error", "-show_streams", "-show_format",
                   "-print_format", "json", str(path)])
    info = json.loads(raw)
    video = next((s for s in info["streams"] if s["codec_type"] == "video"
                  and not s.get("disposition", {}).get("attached_pic")), None)
    if not video:
        raise ClipperError(f"No video track in {path.name}.")
    audio = next((s for s in info["streams"] if s["codec_type"] == "audio"), None)
    def start_time(track, fallback):
        raw_time = track.get("start_time")
        return float(raw_time) if raw_time not in (None, "N/A") else fallback

    origin = start_time(info["format"], 0.0)
    audio_offset = round(1000 * (start_time(audio, origin) - origin)) if audio else 0
    durations = (video.get("duration"), info["format"].get("duration"))
    for index, value in enumerate(durations):
        if value not in (None, "N/A"):
            length = float(value)
            if index == 0:
                length += start_time(video, origin) - origin
            if math.isfinite(length) and length > 0:
                return round(length * 1000), audio is not None, audio_offset
    raise ClipperError(f"Could not determine the video duration of {path.name}.")


def framing_filter(layout, *, cover=False):
    frames = "select='not(mod(n,6))',thumbnail=n=16" if cover else "fps=30"
    base = f"[0:V:0]{frames},scale=w='max(2,round(iw*if(gt(sar,0),sar,1)/2)*2)':h=ih,setsar=1"
    if layout == "crop":
        framing = base + ",scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920"
    else:
        framing = (base + ",split=2[bg][fg];"
                   "[bg]scale=270:480:force_original_aspect_ratio=increase,crop=270:480,"
                   "boxblur=12:2,eq=brightness=-0.14:saturation=0.72,scale=1080:1920[back];"
                   "[fg]scale=1080:1920:force_original_aspect_ratio=decrease:force_divisible_by=2[front];"
                   "[back][front]overlay=(W-w)/2:(H-h)/2")
    return framing + ",setsar=1"


def create_title(clip, source_title):
    text = phrase(clip.words)
    sentences = re.split(r'(?<=[.!?。！？])\s+', text)
    # Pull a self-contained hook directly from the speaker's words.
    def hook_score(line):
        if not 10 <= len(line) <= 95:
            return 0
        if line.endswith(("?", "？")):
            return 3
        if re.match(r"^(how|why|here['’]s|imagine|try)\b", line, re.I) or re.search(r"\b\d+\b", line):
            return 2
        return 1

    hook = max(sentences, key=hook_score)
    title = re.sub(r"[<>\x00-\x1f]", "", hook).strip() or source_title
    if len(title) > 95:
        shortened = title[:94].rsplit(" ", 1)[0].rstrip(" ,")
        title = (shortened or title[:94]) + "…"
    return title


def make_clip(path, clip, media_id, source_title, origin, has_audio, layout, highlight, output, caption_style="pop"):
    # The upload identity stays stable across style changes and reruns.
    upload_key = digest(f"{media_id}/{clip.start}/{clip.end}")[:32]
    words = [(w.start, w.end, w.text, w.boundary) for w in clip.words]
    style = digest(json.dumps(["render-v4", upload_key, layout, highlight, caption_style, words], ensure_ascii=False))[:12]
    folder = output / "clips" / f"{upload_key[:12]}-{style}"
    if all((folder / name).is_file() for name in ("short.mp4", "captions.srt", "thumbnail.jpg", "details.json")):
        return folder, read_json(folder / "details.json")
    folder.parent.mkdir(parents=True, exist_ok=True)
    details = {
        "title": create_title(clip, source_title), "source_title": source_title,
        "description": "",
        "start_seconds": clip.start / 1000, "end_seconds": clip.end / 1000,
        "upload_key": upload_key, "caption_style": caption_style, "thumbnail": "thumbnail.jpg",
    }
    sentences = re.split(r'(?<=[.!?。！？])\s+', phrase(clip.words))
    excerpt = next((s for s in sentences if s != details["title"] and len(s) >= 12), "")
    excerpt = re.sub(r"[<>\x00-\x1f]", "", excerpt).strip()
    if len(excerpt) > 220:
        excerpt = excerpt[:219].rsplit(" ", 1)[0] + "…"
    credit = f"From {source_title}\n{origin}" if origin else ""
    details["description"] = "\n\n".join(part for part in (excerpt, credit, "#Shorts") if part)
    with tempfile.TemporaryDirectory(dir=folder.parent) as tmp:
        stage = Path(tmp) / "render"
        stage.mkdir()
        write_captions(clip, stage, highlight, caption_style)
        args = ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
                "-ss", f"{clip.start / 1000:.3f}", "-i", str(path)]
        if not has_audio:
            args += ["-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo"]
        args += ["-t", f"{(clip.end - clip.start) / 1000:.3f}",
                 "-filter_complex_threads", "2", "-filter_complex",
                 framing_filter(layout) + ",ass=captions.ass,format=yuv420p[v]",
                 "-map", "[v]", "-map", "0:a:0" if has_audio else "1:a:0",
                 "-c:v", "libx264", "-threads", "4", "-preset", "veryfast", "-crf", "21",
                 "-c:a", "aac", "-b:a", "128k", "-ar", "48000", "-ac", "2", "-map_metadata", "-1",
                 "-movflags", "+faststart", "short.mp4"]
        print(f"Rendering {details['start_seconds']:.1f}–{details['end_seconds']:.1f}s …", flush=True)
        execute(args, cwd=stage)
        write_cover_script(details["title"], stage / "cover.ass")
        sample = clip.start + (clip.end - clip.start) // 5
        cover_args = ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
                      "-ss", f"{sample / 1000:.3f}", "-t", f"{min(8000, clip.end - sample) / 1000:.3f}",
                      "-i", str(path), "-filter_complex_threads", "2", "-filter_complex",
                      framing_filter(layout, cover=True) + ",setpts=PTS-STARTPTS,ass=cover.ass[v]",
                      "-map", "[v]", "-frames:v", "1", "-q:v", "2", "-threads", "2",
                      "-map_metadata", "-1", "thumbnail.jpg"]
        execute(cover_args, cwd=stage)
        if not (stage / "thumbnail.jpg").is_file():
            raise ClipperError("Could not extract a source frame for the clip thumbnail.")
        (stage / "captions.ass").unlink()
        (stage / "cover.ass").unlink()
        save_json(stage / "details.json", details)
        if folder.exists():  # Restore missing members of a previously generated bundle.
            for item in stage.iterdir():
                os.replace(item, folder / item.name)
        else:
            os.replace(stage, folder)
    return folder, details
