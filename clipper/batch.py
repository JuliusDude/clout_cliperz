"""Plan a pasted batch before publishing so retries preserve its clip count."""

import json
import re
import sys

from .captions import Clip, highlights
from .media import video_id, youtube_url
from .storage import ClipperError, digest, read_json, save_json
from .worker import finish_clips, prepare_source


def parse_links(text):
    ids = []
    for token in re.split(r"[\s,]+", text.strip()):
        if token:
            try:
                ids.append(video_id(token))
            except (ValueError, ClipperError) as error:
                raise ClipperError(f"Invalid YouTube video link: {token[:160]}") from error
    if not ids:
        raise ClipperError("Paste at least one YouTube video link.")
    return list(dict.fromkeys(ids))


def read_links(args):
    pieces = list(args.urls)
    if args.file:
        pieces.append(args.file.expanduser().read_text(encoding="utf-8-sig"))
    if not pieces:
        if sys.stdin.isatty():
            print("Paste YouTube video links. Press Enter on an empty line to start.", flush=True)
            for line in sys.stdin:
                if not line.strip():
                    break
                pieces.append(line)
        else:
            pieces.append(sys.stdin.read())
    return parse_links("\n".join(pieces))


def allocate(capacities, total):
    counts = dict.fromkeys(capacities, 0)
    while total:
        available = [ident for ident, capacity in capacities.items() if counts[ident] < capacity]
        if not available:
            break
        for ident in available[:total]:
            counts[ident] += 1
            total -= 1
    return counts


def run_batch(ids, args, client, state):
    target = args.clips * len(ids) if args.per_video else args.clips
    key = digest(json.dumps([ids, args.clips, args.per_video, args.seconds, args.model, args.language]))
    marker = state / "batches" / (key + ".json")
    plans = read_json(marker, {})
    remaining = target - sum(len(p["ranges"]) for p in plans.values())
    prepared = {}
    pools = {}
    known_content = {p["media_id"] for p in plans.values()}
    for ident in ids:
        if ident in plans or remaining <= 0:
            continue
        if not args.per_video and len(pools) >= remaining:
            break  # Each source already in the pool can fill at least one slot.
        try:
            source = prepare_source(youtube_url(ident), args, state)
            if source.media_id in known_content:
                plans[ident] = {"media_id": source.media_id, "ranges": []}
                print(f"Skipping duplicate video content: {youtube_url(ident)}", flush=True)
                continue
            count = args.clips if args.per_video else remaining
            pools[ident] = highlights(source.words, source.duration, args.seconds, count)
            prepared[ident] = source
            known_content.add(source.media_id)
        except Exception as error:
            print(f"Could not plan {youtube_url(ident)}: {error}", file=sys.stderr, flush=True)

    capacities = {ident: len(pool) for ident, pool in pools.items()}
    counts = capacities if args.per_video else allocate(capacities, remaining)
    for ident, count in counts.items():
        source = prepared[ident]
        chosen = pools[ident]
        if 0 < count < len(chosen):
            # Refit the awarded number of passages at the preferred duration.
            fitted = highlights(source.words, source.duration, args.seconds, count)
            chosen = fitted if len(fitted) == count else chosen[:count]
        plans[ident] = {"media_id": source.media_id, "ranges": [[c.start, c.end] for c in chosen[:count]]}
    save_json(marker, plans)

    rendered = completed = 0
    for ident in ids:
        plan = plans.get(ident)
        if not plan or not plan["ranges"]:
            continue
        try:
            source = prepared.get(ident) or prepare_source(youtube_url(ident), args, state)
            if source.media_id != plan["media_id"]:
                raise ClipperError("The cached source changed since this batch was planned.")
            clips = []
            for start, end in plan["ranges"]:
                words = tuple(w for w in source.words if w.start >= start and w.end <= end)
                if not words:
                    raise ClipperError("The transcript no longer covers the planned passage.")
                clips.append(Clip(start, end, words))
            made, done = finish_clips(source, clips, args, client, state)
            rendered += made
            completed += done
        except Exception as error:
            print(f"Could not finish {youtube_url(ident)}: {error}", file=sys.stderr, flush=True)
    print(f"{rendered}/{target} requested clips ready.", flush=True)
    if client is not None:
        print(f"{completed}/{target} uploads complete with thumbnails.", flush=True)
    if sum(len(p["ranges"]) for p in plans.values()) < target:
        print("The available sources supplied fewer distinct passages than requested.", file=sys.stderr, flush=True)
    return 0 if completed == target else 1
