"""Transcription, speech-boundary highlight selection and subtitle layout."""

import html
import math
import os
import re
import unicodedata
from dataclasses import astuple, dataclass, replace
from functools import lru_cache

from .storage import ClipperError, digest, read_json, save_json


@dataclass(frozen=True)
class Word:
    start: int  # milliseconds on the source timeline
    end: int
    text: str
    boundary: bool = False


@dataclass(frozen=True)
class Clip:
    start: int
    end: int
    words: tuple


CJK = "\u3040-\u30ff\u3400-\u9fff"
STOP = re.compile(r"""[.!?…。！？]["'”’)\]]*$""")
STAMP = r"(\d+):([0-5]\d):([0-5]\d)[,.](\d{1,3})"
TIMING = re.compile(STAMP + r"\s*-->\s*" + STAMP)


def milliseconds(parts):
    h, m, s, fraction = parts
    return ((int(h) * 60 + int(m)) * 60 + int(s)) * 1000 + int(fraction.ljust(3, "0"))


def distribute(text, start, end):
    # SRT provides cue timing. Estimate positions only to split long cues.
    tokens = re.findall(f"[{CJK}]|[^\\s{CJK}]+", text)
    weights = [max(2, len(token)) for token in tokens]
    boundary = start
    so_far = 0
    total = sum(weights)
    for i, (token, weight) in enumerate(zip(tokens, weights)):
        so_far += weight
        next_boundary = start + round((end - start) * so_far / total)
        yield Word(boundary, next_boundary, token, i == len(tokens) - 1)
        boundary = next_boundary


def load_srt(path):
    blocks = re.split(r"\n[ \t]*\n", path.read_text(encoding="utf-8-sig").strip())
    words = []
    for block in blocks:
        lines = block.splitlines()
        for i, line in enumerate(lines):
            match = TIMING.match(line.strip())
            if match:
                parts = match.groups()
                start, end = milliseconds(parts[:4]), milliseconds(parts[4:])
                text = re.sub(r"</?[^>]+>", "", " ".join(lines[i + 1:]))
                if end > start:
                    words.extend(distribute(html.unescape(text).strip(), start, end))
                break
    if not words:
        raise ClipperError(f"No timed caption text in {path}. Use a UTF-8 SubRip (.srt) file.")
    return words


@lru_cache(maxsize=1)
def speech_model(name):
    try:
        from faster_whisper import WhisperModel
    except ImportError as error:
        raise ClipperError("Install requirements.txt for speech recognition, or supply a matching .srt file.") from error
    return WhisperModel(name, device="cpu", compute_type="int8", cpu_threads=min(4, os.cpu_count() or 1))


def transcript(path, subtitle, source_id, state, model, language, duration, audio_offset):
    if subtitle is not None:
        words = load_srt(subtitle)
    else:
        key = digest(f"words-v1/{source_id}/{model}/{language}")
        cache = state / "transcripts" / (key + ".json")
        stored = read_json(cache)
        if stored is None:
            print(f"Transcribing {path.name} with {model} …", flush=True)
            segments, _ = speech_model(model).transcribe(
                str(path), language=language, word_timestamps=True, vad_filter=True,
                beam_size=3, condition_on_previous_text=False,
            )
            words = []
            for segment in segments:
                spoken = [w for w in (segment.words or []) if w.word.strip()]
                for i, word in enumerate(spoken):
                    words.append(Word(round(word.start * 1000), round(word.end * 1000),
                                      word.word.strip(), i == len(spoken) - 1))
            if words:
                save_json(cache, [astuple(w) for w in words])
        else:
            words = [Word(*row) for row in stored]
    clean = []
    # Whisper timestamps start at the first decoded audio sample. Camera and
    # screen recordings sometimes start the audio track after the video track.
    shift = 0 if subtitle is not None else audio_offset
    for word in sorted(words, key=lambda w: w.start):
        start = max(word.start + shift, clean[-1].end if clean else 0)
        end = min(duration, word.end + shift)
        if word.text and end > start:
            clean.append(replace(word, start=start, end=end))
    if not clean:
        raise ClipperError("No timed speech was found in the video. Supply a matching .srt to caption it.")
    return clean


def separator(left, right):
    if not left or not right or right[0] in ",.!?;:%)]}’。，、！？":
        return ""
    if re.search(f"[{CJK}]$", left) or re.match(f"[{CJK}]", right):
        return ""
    return " "


def phrase(words):
    parts = []
    previous = ""
    for word in words:
        parts.extend((separator(previous, word.text), word.text))
        previous = word.text
    return "".join(parts)


def highlights(words, duration, seconds, count):
    maximum = seconds * 1000
    groups = []
    first = 0
    for i, word in enumerate(words):
        pause = i + 1 == len(words) or words[i + 1].start - word.end >= 600
        if pause or word.boundary or STOP.search(word.text) or word.end - words[first].start >= min(7500, maximum // 2):
            groups.append((first, i + 1))
            first = i + 1

    # Rank contiguous passages by a clean opening/ending, speech density and
    # proximity to the requested length. Never bridge a long silent break.
    options = []
    for g, (lo, _) in enumerate(groups):
        speech = 0
        vocab = set()
        for h in range(g, len(groups)):
            begin, hi = groups[h]
            if h > g and words[begin].start - words[begin - 1].end > 2200:
                break
            a, b = words[lo].start, words[hi - 1].end
            if b - a > maximum:
                break
            for w in words[begin:hi]:
                speech += w.end - w.start
                vocab.add(w.text.casefold().strip(".,!?"))
            start = max(0, a - 100, b - maximum, (words[lo - 1].end + a) // 2 if lo else 0)
            tail = (b + words[hi].start) // 2 if hi < len(words) else duration
            end = min(duration, b + 180, tail, start + maximum)
            length = b - a
            clean_start = lo == 0 or STOP.search(words[lo - 1].text) or a - words[lo - 1].end >= 600
            clean_end = bool(STOP.search(words[hi - 1].text)) or hi == len(words)
            hook = re.match(r"^(why|how|what|imagine|here['’]s|the secret)\b", phrase(words[lo:min(hi, lo+3)]), re.I)
            score = (3.5 * length / maximum + 0.9 * bool(clean_start) + 1.15 * clean_end
                     + 0.55 * bool(hook) + 0.7 * speech / max(1, length)
                     + 0.30 * min(1.0, math.log1p(len(vocab)) / 3))
            options.append((score, start, end, lo, hi))

    if not options:
        raise ClipperError("Could not fit a captioned passage into the requested clip length.")
    longest = max(end - start for _, start, end, _, _ in options)
    minimum = min(maximum * 0.48, longest * 0.90)
    selected = []
    for _, start, end, lo, hi in sorted(options, key=lambda o: (-o[0], o[1])):
        if end - start < minimum or any(start < c.end and end > c.start for c in selected):
            continue
        selected.append(Clip(start, end, tuple(words[lo:hi])))
        if len(selected) == count:
            break
    return sorted(selected, key=lambda c: c.start)


def width(text):
    return sum(2 if unicodedata.east_asian_width(c) in ("W", "F") else 1 for c in text)


def caption_groups(words):
    # Choose breaks across each spoken phrase together, so a greedy split
    # cannot leave the final word flashing alone on the next caption.
    first = 0
    for i, word in enumerate(words):
        end_phrase = word.boundary or STOP.search(word.text) or i + 1 == len(words)
        if not end_phrase and words[i + 1].start - word.end <= 360:
            continue
        span = words[first:i + 1]
        n = len(span)
        costs = [float("inf")] * n + [0.0]
        breaks = [0] * n
        for a in range(n - 1, -1, -1):
            for b in range(a + 1, min(n, a + 7) + 1):
                chars = width(phrase(span[a:b]))
                seconds = (span[b - 1].end - span[a].start) / 1000
                if b > a + 1 and (chars > 36 or seconds > 2.8):
                    break
                cost = 1.0 + ((chars - 24) / 22) ** 2 + ((seconds - 1.6) / 1.7) ** 2
                if n > 2 and b - a <= 2:
                    cost += 3.0 if b - a == 1 else 0.8
                if b < n:
                    last = span[b - 1].text.casefold()
                    if last in {"a", "an", "the", "of", "to", "and", "from", "that"}:
                        cost += 1.1
                    if last.endswith((",", "，", "—")):
                        cost -= 1.0
                if cost + costs[b] < costs[a]:
                    costs[a], breaks[a] = cost + costs[b], b
        a = 0
        while a < n:
            yield span[a:breaks[a]]
            a = breaks[a]
        first = i + 1


def line_split(group):
    if width(phrase(group)) <= 22 or len(group) < 2:
        return len(group)
    return min(range(1, len(group)), key=lambda i: abs(width(phrase(group[:i])) - width(phrase(group[i:]))))


def clock(ms, ass=False):
    unit = 100 if ass else 1000
    # Quantize ASS boundaries consistently so adjacent words share a boundary.
    value = (ms + 5) // 10 if ass else ms
    whole, fraction = divmod(max(0, value), unit)
    mins, sec = divmod(whole, 60)
    hrs, minute = divmod(mins, 60)
    if ass:
        return f"{hrs}:{minute:02}:{sec:02}.{fraction:02}"
    return f"{hrs:02}:{minute:02}:{sec:02},{fraction:03}"


ASS_HEADER = """[Script Info]
ScriptType: v4.00+
PlayResX: 1080
PlayResY: 1920
WrapStyle: 0
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Caption,Arial,76,&H00FFFFFF,&H00FFFFFF,&H00111218,&H80111218,-1,0,0,0,100,100,0,0,1,4.4,1.5,2,118,154,438,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""


def ass_text(group, split, active):
    parts = []
    for i, w in enumerate(group):
        if i:
            parts.append(r"\N" if i == split else separator(group[i - 1].text, w.text))
        text = w.text.replace("\\", "＼").replace("{", "｛").replace("}", "｝")
        parts.append(r"{\c&H69E8FF&}" + text + r"{\r}" if i == active else text)
    return "".join(parts)


def write_captions(clip, folder, word_highlighting):
    srt, events = [], [ASS_HEADER]
    for n, group in enumerate(caption_groups(clip.words), 1):
        split = line_split(group)
        text = "\n".join(phrase(part) for part in (group[:split], group[split:]) if part)
        start, end = group[0].start - clip.start, group[-1].end - clip.start
        srt.append(f"{n}\n{clock(start)} --> {clock(end)}\n{text}\n\n")
        for i in (range(len(group)) if word_highlighting else [None]):
            a = group[i].start - clip.start if i is not None else start
            b = group[i + 1].start - clip.start if i is not None and i + 1 < len(group) else end
            if clock(a, True) != clock(b, True):
                line = ass_text(group, split, i)
                events.append(f"Dialogue: 0,{clock(a, True)},{clock(b, True)},Caption,,0,0,0,,{line}\n")
    (folder / "captions.srt").write_text("".join(srt), encoding="utf-8")
    (folder / "captions.ass").write_text("".join(events), encoding="utf-8")
