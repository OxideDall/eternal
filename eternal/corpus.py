"""Frozen corpus: transcript prefixes pinned by (path, byte length, sha256) and read back exactly."""
import hashlib
import json
import os
from collections.abc import Iterator, Mapping
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path

CHUNK = 1 << 20
TAIL = 1 << 16


class Digest(StrEnum):
    PREFIX = "prefix"  # sha256 of the whole prefix: frozen corpora
    TAIL = "tail"  # size plus the last TAIL bytes: live archives, catches truncation and rewrites near the end


@dataclass(frozen=True, slots=True)
class Frozen:
    path: str  # relative to the manifest root
    size: int  # bytes through the last newline at freeze time
    sha256: str
    sub: bool  # a subagent transcript

    @property
    def session(self) -> str:
        return Path(self.path).stem

    @property
    def project(self) -> str:
        return Path(self.path).parts[0]


@dataclass(frozen=True, slots=True)
class Manifest:
    root: str
    created: str
    files: tuple[Frozen, ...]

    def digest(self) -> str:
        return hashlib.sha256(self.dumps().encode()).hexdigest()

    def dumps(self) -> str:
        return json.dumps({"root": self.root, "created": self.created, "files": [asdict(f) for f in self.files]},
                          ensure_ascii=False, indent=1)

    @classmethod
    def load(cls, path: Path) -> "Manifest":
        d = json.loads(Path(path).read_text())
        return cls(d["root"], d["created"], tuple(Frozen(**f) for f in d["files"]))


def _last_newline(path: Path) -> int:
    """Length of the file through its last newline (0 if none)."""
    with open(path, "rb") as f:
        end = f.seek(0, os.SEEK_END)
        pos = end
        while pos > 0:
            step = min(CHUNK, pos)
            pos -= step
            f.seek(pos)
            k = f.read(step).rfind(b"\n")
            if k >= 0:
                return pos + k + 1
    return 0


def prefix_sha(path: Path, size: int) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        left = size
        while left:
            b = f.read(min(CHUNK, left))
            if not b:
                raise ValueError(f"{path}: shorter than frozen size {size}")
            h.update(b)
            left -= len(b)
    return h.hexdigest()


def tail_sha(path: Path, size: int) -> str:
    start = max(0, size - TAIL)
    with open(path, "rb") as f:
        f.seek(start)
        b = f.read(size - start)
    if len(b) != size - start:
        raise ValueError(f"{path}: shorter than {size}")
    return hashlib.sha256(size.to_bytes(8, "big") + b).hexdigest()


DIGESTS = {Digest.PREFIX: prefix_sha, Digest.TAIL: tail_sha}


def freeze(root: Path, sub: bool = False, exclude: frozenset[str] = frozenset(),
           digest: Digest = Digest.PREFIX, unchanged: Mapping[str, int] | None = None) -> Manifest:
    """Pin every transcript under root; `exclude` drops sessions by file stem, `unchanged` files still of that size."""
    root = Path(root).expanduser()
    files = []
    for path in sorted(root.rglob("*.jsonl")):
        rel = path.relative_to(root).as_posix()
        is_sub = "/subagents/" in f"/{rel}"
        if (is_sub and not sub) or path.stem in exclude:
            continue
        try:
            if unchanged and unchanged.get(rel) == path.stat().st_size:
                continue
            size = _last_newline(path)
            if size:
                files.append(Frozen(rel, size, DIGESTS[digest](path, size), is_sub))
        except (OSError, ValueError):  # deleted or truncated while reading
            continue
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    return Manifest(str(root), now, tuple(files))


def verify(m: Manifest) -> list[str]:
    """Paths whose frozen prefix is gone or changed."""
    bad = []
    for f in m.files:
        p = Path(m.root) / f.path
        try:
            if p.stat().st_size < f.size or prefix_sha(p, f.size) != f.sha256:
                bad.append(f.path)
        except (OSError, ValueError):
            bad.append(f.path)
    return bad


def lines(m: Manifest, f: Frozen, start: int = 0) -> Iterator[tuple[int, bytes]]:
    """(byte offset, line) pairs of the frozen prefix from `start`, without trailing newlines."""
    with open(Path(m.root) / f.path, "rb") as fh:
        fh.seek(start)
        pos = start
        for raw in fh:
            if pos + len(raw) > f.size:
                break
            yield pos, raw.rstrip(b"\n")
            pos += len(raw)
