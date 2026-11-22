#!/usr/bin/env python3
"""Create captioned Shorts and automatically publish them to YouTube."""

import argparse
import sys
from pathlib import Path

from clipper import publisher
from clipper.media import check_tools
from clipper.storage import ClipperError, work_lock
from clipper.worker import run_source, watch


def bounded(low, high):
    def parse(value):
        number = int(value)
        if not low <= number <= high:
            raise argparse.ArgumentTypeError(f"choose an integer from {low} to {high}")
        return number
    return parse


def arguments():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    auth = commands.add_parser("connect", help="authorize the destination YouTube channel once")
    auth.add_argument("client_file", type=Path, help="Google Desktop OAuth client JSON")
    recovery = commands.add_parser("reconcile", help="resolve an uncertain final upload after checking Studio")
    recovery.add_argument("clip_folder", type=Path, help="generated folder containing details.json")
    outcome = recovery.add_mutually_exclusive_group(required=True)
    outcome.add_argument("--video-id", help="record the existing upload ID from Studio")
    outcome.add_argument("--retry", action="store_true", help="confirm Studio has no upload and allow a fresh attempt")
    single = commands.add_parser("run", help="process one local video or YouTube video URL")
    single.add_argument("source", help="video file or YouTube video URL")
    single.add_argument("--srt", type=Path, help="use supplied SubRip captions")
    watcher = commands.add_parser("watch", help="automatically process a folder or YouTube channel feed")
    watcher.add_argument("source", help="inbox directory, YouTube channel URL, @handle, or UC… channel ID")
    watcher.add_argument("--once", action="store_true", help="scan once and exit (for a scheduler)")
    watcher.add_argument("--interval", type=bounded(5, 86400), default=60,
                         help="seconds between scans (default 60)")
    watcher.add_argument("--include-latest", action="store_true",
                         help="also process the latest existing upload when first registering a channel")
    for command in (single, watcher):
        command.add_argument("--output", type=Path, default=Path("shorts-output"),
                             help="persistent clips, source cache and posting history")
        command.add_argument("--clips", type=bounded(1, 20), default=1,
                             help="maximum number of distinct highlights per source (default 1)")
        command.add_argument("--seconds", type=bounded(5, 180), default=35,
                             help="maximum clip duration in seconds (default 35)")
        command.add_argument("--layout", choices=["fit", "crop"], default="fit",
                             help="keep full picture over blurred fill, or center-crop (default fit)")
        command.add_argument("--model", default="base", help="faster-whisper model name or local directory")
        command.add_argument("--language", help="spoken language code, e.g. en or es (default auto-detect)")
        command.add_argument("--publish", choices=["private", "unlisted", "public"],
                             help="upload using this visibility (default saves locally)")
        command.add_argument("--audience", choices=["kids", "general"],
                             help="made-for-kids setting for uploads (default uses channel settings)")
    return parser.parse_args()


def main():
    args = arguments()
    if args.command == "connect":
        publisher.connect(args.client_file.expanduser())
        return 0
    if args.command == "reconcile":
        publisher.reconcile(args.clip_folder, args.video_id)
        return 0
    check_tools()
    args.output = args.output.expanduser().resolve()
    state = args.output / ".state"
    client = publisher.session() if args.publish else None
    try:
        if args.command == "watch":
            return watch(args, client, state)
        with work_lock(state):
            run_source(args.source, args, client, state)
        return 0
    finally:
        if client is not None:
            client.close()


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("Worker stopped.", file=sys.stderr)
        sys.exit(130)
    except (ClipperError, OSError, ValueError, RuntimeError) as error:
        print(f"Error: {error}", file=sys.stderr)
        sys.exit(1)
