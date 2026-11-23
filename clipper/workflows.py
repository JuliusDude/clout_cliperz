"""Names and output locations for the two Shorts workflows."""

import os
from pathlib import Path

CLOUT_CUTS = "clout-cuts"
MOVIE_RECAP = "movie-recap"
STREAMERS = "streamers"
PODCAST = "podcast"
DYNAMIC_MODES = (STREAMERS, PODCAST)
CLOUT_HANDLE = "@Clout-CutsTV"
WORKFLOWS = {CLOUT_CUTS: "Clout-Cuts", MOVIE_RECAP: "Movie-Recap", STREAMERS: "Streamers", PODCAST: "Podcast"}
DEFAULT_OUTPUT = Path("G:/") if os.name == "nt" else Path("shorts-output")


def workflow_output(base, workflow):
    return base / WORKFLOWS[CLOUT_CUTS if workflow in DYNAMIC_MODES else workflow]


def default_seconds(workflow):
    return {STREAMERS: 60, PODCAST: 120}.get(workflow, 35)


def default_caption(workflow):
    return {STREAMERS: "stream-pop", PODCAST: "classic"}.get(workflow, "pop")


def ensure_workflow_folders(output):
    for name in ("originals", "clips", "thumbnails"):
        (output / name).mkdir(parents=True, exist_ok=True)
