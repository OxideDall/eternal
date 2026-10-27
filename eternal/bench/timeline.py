"""Per-session view of the archive for benches: rows in transcript order plus compaction cuts."""
import json
import sqlite3
from collections.abc import Iterator
from dataclasses import dataclass

from ..facts import Row, norm
from ..kinds import Kind, Trigger, Via

AUM = "all user messages"
AFTER_AUM = ("pending tasks", "current work", "optional next step")


@dataclass(frozen=True, slots=True)
class Cut:
    """A compaction boundary closing `epoch`, with its normalized summary."""

    epoch: int
    line: int
    trigger: Trigger
    preserved: frozenset[str]
    summary: str | None
    aum: tuple[int, int] | None  # span of the "All user messages" section in `summary`
    ts: str | None = None
    version: str | None = None


@dataclass(frozen=True, slots=True)
class Timeline:
    source_id: int
    session: str
    project: str
    rows: tuple[Row, ...]
    cuts: tuple[Cut, ...]


def aum_span(summary: str) -> tuple[int, int] | None:
    start = summary.find(AUM)
    if start < 0:
        return None
    ends = [k for k in (summary.find(h, start) for h in AFTER_AUM) if k >= 0]
    return start, min(ends) if ends else len(summary)


def sources(db: sqlite3.Connection, with_cuts: bool = False) -> list[tuple[int, str, str]]:
    sql = "SELECT id, session, project FROM source WHERE sub = 0"
    if with_cuts:
        sql += " AND id IN (SELECT source_id FROM boundary)"
    return list(db.execute(sql + " ORDER BY id"))


def load(db: sqlite3.Connection, source_id: int, kinds: frozenset[Kind] | None = None) -> Timeline:
    _, session, project = db.execute("SELECT id, session, project FROM source WHERE id = ?", (source_id,)).fetchone()
    rows = []
    for rid, line, kind, epoch, uuid, ts, tool, tuid, args, text, via in db.execute(
            "SELECT id, line, kind, epoch, uuid, ts, tool, tool_use_id, args, text, via FROM record "
            "WHERE source_id = ? ORDER BY line, block", (source_id,)):
        k = Kind(kind)
        if kinds is None or k in kinds:
            rows.append(Row(rid, line, k, epoch, uuid, ts, tool, tuid, json.loads(args) if args else None, text,
                            Via(via) if via else None))
    cuts = []
    for epoch, line, trig, pres, summary, ts, version in db.execute(
            "SELECT b.epoch, b.line, b.trigger, b.preserved, r.text, b.ts, b.version FROM boundary b "
            "LEFT JOIN record r ON r.id = b.summary_id WHERE b.source_id = ? ORDER BY b.line", (source_id,)):
        s = norm(summary) if summary else None
        cuts.append(Cut(epoch, line, Trigger(trig), frozenset(json.loads(pres)), s, aum_span(s) if s else None,
                        ts, version))
    return Timeline(source_id, session, project, tuple(rows), tuple(cuts))


def timelines(db: sqlite3.Connection, with_cuts: bool = False,
              kinds: frozenset[Kind] | None = None) -> Iterator[Timeline]:
    for sid, _, _ in sources(db, with_cuts):
        yield load(db, sid, kinds)
