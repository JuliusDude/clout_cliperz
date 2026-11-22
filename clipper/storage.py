"""Small, crash-safe files shared by the renderer and publisher."""

import hashlib
import json
import os
import tempfile
from contextlib import contextmanager
from pathlib import Path


class ClipperError(Exception):
    pass


def read_json(path, default=None):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default
    except (ValueError, UnicodeError) as error:
        raise ClipperError(f"Could not read {path}. Restore it from a backup before continuing.") from error


def save_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".writing-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as out:
            json.dump(value, out, ensure_ascii=False, indent=2)
            out.write("\n")
            out.flush()
            os.fsync(out.fileno())
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def digest(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def file_hash(path):
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def signature(path):
    info = path.stat()
    return [info.st_size, info.st_mtime_ns, info.st_ctime_ns]


def cached_hash(path, state):
    before = signature(path)
    index = state / "inputs" / (digest(str(path)) + ".json")
    saved = read_json(index)
    if saved and saved["signature"] == before:
        return saved["hash"]
    result = file_hash(path)
    if signature(path) != before:
        raise ClipperError(f"{path.name} is still changing. It can be retried when the copy finishes.")
    save_json(index, {"signature": before, "hash": result})
    return result


@contextmanager
def work_lock(state):
    # OS locks release automatically if the worker exits or is killed.
    state.mkdir(parents=True, exist_ok=True)
    with (state / "worker.lock").open("a+b") as handle:
        try:
            if os.name == "nt":
                import msvcrt
                if handle.tell() == 0:
                    handle.write(b"\0")
                    handle.flush()
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            raise ClipperError("Another worker is using this output folder.") from error
        yield
