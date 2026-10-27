"""Facts a session must keep across compactions: human prompts and written files, extracted by code from records."""
import os
import re
from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum

from .classify import WRITE_TOOLS
from .kinds import Kind, Via

PROBE = 60  # chars of a prompt that must appear verbatim, as in claudevs
MIN_PROMPT = 20
SPACE = re.compile(r"\s+")
IDE_CONTEXT = re.compile(r"<(ide_[a-z_]+)>.*?</\1>", re.S)  # what the IDE attached, not what the human typed


def own_words(text: str) -> str:
    """A human prompt without the IDE context attached to it."""
    return IDE_CONTEXT.sub("", text).strip()


def norm(text: str) -> str:
    return SPACE.sub(" ", text).casefold().strip()


@dataclass(frozen=True, slots=True)
class Row:
    """One record of a session in transcript order; `line` is the 0-based transcript line."""

    id: int
    line: int
    kind: Kind
    epoch: int
    uuid: str | None
    ts: str | None
    tool: str | None
    tool_use_id: str | None
    args: dict | None
    text: str
    via: Via | None = None


class FactKind(StrEnum):
    REQUEST = "request"
    FILE = "file"
    TASK = "task"


@dataclass(frozen=True, slots=True)
class Fact:
    kind: FactKind
    probe: str
    epoch: int
    row: int
    line: int
    uuid: str | None
    ts: str | None
    text: str
    via: Via | None = None
    files: tuple[str, ...] = ()  # basenames a request's turn wrote, in order, until the next human message


def written(r: Row) -> str | None:
    if r.kind == Kind.TOOL_USE and r.tool in WRITE_TOOLS and r.args:
        p = r.args.get("file_path") or r.args.get("notebook_path")
        return p if isinstance(p, str) and p else None
    return None


def turn_files(rows: list[Row]) -> dict[int, tuple[str, ...]]:
    """Per human row id, distinct basenames written after it in the same epoch before the next human message."""
    out: dict[int, list[str]] = {}
    cur: Row | None = None
    for r in rows:
        if r.kind == Kind.HUMAN:
            cur = r
            out[r.id] = []
        elif cur is not None and r.epoch == cur.epoch and (p := written(r)):
            base = os.path.basename(p)
            if base not in out[cur.id]:
                out[cur.id].append(base)
    return {k: tuple(v) for k, v in out.items() if v}


def facts(rows: Iterable[Row]) -> list[Fact]:
    """Human requests (with the files their turn wrote) and written files per epoch, deduplicated by probe."""
    rows = list(rows)
    turns = turn_files(rows)
    out, seen = [], set()
    for r in rows:
        if r.kind == Kind.HUMAN:
            body = own_words(r.text)
            if len(body) < MIN_PROMPT:
                continue
            f = Fact(FactKind.REQUEST, norm(body[:PROBE]), r.epoch, r.id, r.line, r.uuid, r.ts, body, r.via,
                     turns.get(r.id, ()))
        elif r.kind == Kind.TOOL_USE and r.tool in WRITE_TOOLS and r.args:
            path = written(r)
            if not path:
                continue
            f = Fact(FactKind.FILE, os.path.basename(path).casefold(), r.epoch, r.id, r.line, r.uuid, r.ts, path)
        else:
            continue
        key = (f.kind, f.epoch, f.probe)
        if f.probe and key not in seen:
            seen.add(key)
            out.append(f)
    return out
