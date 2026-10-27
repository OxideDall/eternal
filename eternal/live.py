"""Live archive of every main transcript under ~/.claude/projects, appended incrementally under one lock."""
import fcntl
import getpass
import json
import os
import re
import sqlite3
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from pathlib import Path

from . import archive, corpus

ROOT = Path.home() / ".claude/projects"
EXCLUDED = "excluded"  # session ids kept out of live search, one per line


def live_dir() -> Path:
    return Path(os.environ.get("ETERNAL_LIVE") or f"/var/tmp/{getpass.getuser()}-eternal-live")


def db_path() -> Path:
    return live_dir() / "archive.db"


def project_dir(cwd: Path, root: Path = ROOT) -> Path:
    """Where Claude Code keeps transcripts and auto-memory of sessions started in `cwd`."""
    return root / re.sub(r"[^A-Za-z0-9]", "-", str(cwd.resolve()))


def excluded() -> tuple[str, ...]:
    """Session ids kept out of live search, such as selftest sessions."""
    try:
        text = (live_dir() / EXCLUDED).read_text()
    except OSError:
        return ()
    return tuple(dict.fromkeys(x for x in (y.strip().lower() for y in text.splitlines()) if x))


def exclude(ids: Iterable[str]) -> None:
    new = [i.lower() for i in dict.fromkeys(ids) if i and i.lower() not in excluded()]
    if new:
        live_dir().mkdir(mode=0o700, parents=True, exist_ok=True)
        with os.fdopen(os.open(live_dir() / EXCLUDED, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600), "a") as f:
            f.writelines(f"{i}\n" for i in new)


@contextmanager
def _lock(block: bool) -> Iterator[bool]:
    live_dir().mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(live_dir() / "ingest.lock", os.O_RDWR | os.O_CREAT, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | (0 if block else fcntl.LOCK_NB))
        except BlockingIOError:
            yield False
            return
        yield True
    finally:
        os.close(fd)


def refresh(db: sqlite3.Connection, root: Path = ROOT, block: bool = False, sub: bool = False) -> archive.Stats | None:
    """Append new transcript lines; None when another process is ingesting and `block` is false."""
    with _lock(block) as got:
        if not got:
            return None
        same = dict(db.execute("SELECT path, bytes FROM source"))
        m = corpus.freeze(root, sub, digest=corpus.Digest.TAIL, unchanged=same)
        st = archive.ingest(db, m, digest=corpus.Digest.TAIL, skip_changed=True)
        if st.skipped and not set(st.skipped) <= set(diverged(db)):
            db.execute("INSERT OR REPLACE INTO meta VALUES ('diverged', ?)",
                       (json.dumps(sorted(set(diverged(db)) | set(st.skipped))),))
            db.commit()
        return st


def diverged(db: sqlite3.Connection) -> list[str]:
    """Transcripts whose already ingested part changed; the live archive stopped following them."""
    row = db.execute("SELECT value FROM meta WHERE key = 'diverged'").fetchone()
    return json.loads(row[0]) if row else []
