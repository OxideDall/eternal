"""B1: what survives compaction across summary generations, replayed offline from the frozen archive."""
import re
import sqlite3
import unicodedata
from bisect import bisect_right
from collections import Counter, defaultdict
from collections.abc import Iterable
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum
from typing import TypedDict

from ..archive import Index, quote, search
from ..facts import PROBE, Fact, FactKind, facts, norm
from ..kinds import TURN_STARTS, Kind, Trigger, Via
from ..registry import BUDGETS, Policy, entry_size, latest, params, select
from .timeline import Cut, Timeline

GENS = (0, 1, 2, 3, 5, 10, 20, 50)
TASK_PROBE = 40
TOP_K = 5
RECENT_DAYS = 30
WORD = re.compile(r"\w{4,}")
VERSION = re.compile(r"(\d+)\.(\d+)\.(\d+)")


class TaskStatus(StrEnum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    DELETED = "deleted"


CLOSED = frozenset({TaskStatus.COMPLETED, TaskStatus.DELETED})


class Column(StrEnum):
    """Coverage columns: summary alone, summary plus a registry, and whether every fact fits the budget."""

    SUMMARY = "summary"
    NEWEST = "summary+newest"
    LOG = "summary+log"  # the hook uses this for prompts, NEWEST for files
    NEWEST_MISSING = "summary+newest/missing"
    LOG_MISSING = "summary+log/missing"
    FITS = "registry_fits"


# column -> (selection policy, pick only requests missing from the cut's summary and preserved set)
REGISTRY: dict[Column, tuple[Policy, bool]] = {
    Column.NEWEST: (Policy.NEWEST, False), Column.LOG: (Policy.LOG, False),
    Column.NEWEST_MISSING: (Policy.NEWEST, True), Column.LOG_MISSING: (Policy.LOG, True)}


class Wake(StrEnum):
    """Messages a UserPromptSubmit hook is assumed to fire on."""

    HUMAN = "human"
    TURN = "turn"  # any turn start: human, task notification, peer, scheduled


WAKES = {Wake.HUMAN: frozenset({Kind.HUMAN}), Wake.TURN: TURN_STARTS}


class Fate(StrEnum):
    DELIVERED = "delivered"
    OVERWRITTEN = "overwritten"
    EOF = "eof"


class Query(StrEnum):
    PHRASE = "phrase"
    RARE3 = "rare3"
    STEM5 = "stem5"
    TRIGRAM = "trigram"


class Quantiles(TypedDict, total=False):
    n: int
    p10: float
    p50: float
    p90: float
    max: float


@dataclass(frozen=True, slots=True)
class Slice:
    """A retention tally: one fact kind, optionally narrowed to a delivery path, a version bucket or recent cuts."""

    kind: FactKind
    via: Via | None = None
    version: str | None = None
    recent: bool = False

    def key(self) -> str:
        parts = [self.kind.value]
        if self.via:
            parts.append(f"via={self.via.value}")
        if self.version:
            parts.append(f"version={self.version}")
        if self.recent:
            parts.append("recent")
        return "/".join(parts)


@dataclass(frozen=True, slots=True)
class Cell:
    """A coverage tally: one column for one fact kind, over all cuts or only recent ones."""

    column: Column
    recent: bool = False
    kind: FactKind = FactKind.REQUEST

    def key(self) -> str:
        kind = "" if self.kind == FactKind.REQUEST else f"{self.kind.value}/"
        return ("recent/" if self.recent else "") + kind + self.column.value


@dataclass
class Tally:
    total: int = 0
    summary: int = 0  # probe found in the summary
    kept: int = 0  # summary or a preserved verbatim message
    aum: int = 0  # summary hits inside "All user messages"
    sessions: int = 0
    macro: float = 0.0  # per-session summary share, averaged over sessions


def version_bucket(version: str | None) -> str:
    m = VERSION.match(version or "")
    if not m:
        return "?"
    b = int(m.group(3)) // 50 * 50
    return f"{m.group(1)}.{m.group(2)}.{b}-{b + 49}"


def version_order(bucket: str) -> list[int]:
    return [int(x) for x in re.findall(r"\d+", bucket)] or [1 << 30]


def recent_cutoff(created: str) -> str:
    return (datetime.fromisoformat(created) - timedelta(days=RECENT_DAYS)).isoformat()


@dataclass
class _Task:
    subject: str
    epoch: int
    row: int
    line: int
    uuid: str | None
    ts: str | None
    changes: list[tuple[int, bool]] = field(default_factory=list)  # (epoch, open)


def open_tasks(tl: Timeline) -> list[list[Fact]]:
    """Per cut, the Task* tasks still open when it happened."""
    creates, tasks = {}, {}
    for r in tl.rows:
        if r.kind == Kind.TOOL_USE and r.tool == "TaskCreate" and r.args and r.args.get("subject"):
            creates[r.tool_use_id] = r
        elif r.kind == Kind.TOOL_RESULT and r.tool_use_id in creates and r.args and r.args.get("task_id"):
            c = creates.pop(r.tool_use_id)
            tasks[r.args["task_id"]] = _Task(c.args["subject"], c.epoch, c.id, c.line, c.uuid, c.ts)
        elif r.kind == Kind.TOOL_USE and r.tool == "TaskUpdate" and r.args and r.args.get("status"):
            t = tasks.get(str(r.args.get("taskId")))
            if t:
                t.changes.append((r.epoch, r.args["status"] not in CLOSED))
    out = []
    for cut in tl.cuts:
        now = []
        for t in tasks.values():
            if t.epoch > cut.epoch:
                continue
            state = True
            for ep, is_open in t.changes:
                if ep <= cut.epoch:
                    state = is_open
            if state:
                now.append(Fact(FactKind.TASK, norm(t.subject[:TASK_PROBE]), t.epoch, t.row, t.line, t.uuid, t.ts,
                                t.subject))
        out.append(now)
    return out


def retention(tls: Iterable[Timeline], cutoff: str | None = None) -> dict[Slice, dict[int, Tally]]:
    """Tallies per fact kind; requests also per delivery path, per version bucket and for cuts since `cutoff`."""
    res: dict[Slice, dict[int, Tally]] = defaultdict(lambda: defaultdict(Tally))
    per: dict[tuple[Slice, int], dict[str, list[int]]] = defaultdict(lambda: defaultdict(lambda: [0, 0]))

    def count(f: Fact, cut: Cut, g: int, session: str) -> None:
        if g not in GENS or cut.summary is None:
            return
        hit = f.probe in cut.summary
        kept = hit or f.uuid in cut.preserved
        aum = hit and cut.aum is not None and cut.summary.find(f.probe, *cut.aum) >= 0
        slices = [Slice(f.kind)]
        if f.kind == FactKind.REQUEST:
            slices.append(Slice(f.kind, version=version_bucket(cut.version)))
            if f.via:
                slices.append(Slice(f.kind, via=f.via))
            if cutoff and (cut.ts or "") >= cutoff:
                slices.append(Slice(f.kind, recent=True))
        for s in slices:
            t = res[s][g]
            t.total += 1
            t.summary += hit
            t.kept += kept
            t.aum += aum
            p = per[s, g][session]
            p[0] += hit
            p[1] += 1

    for tl in tls:
        by_epoch = {c.epoch: c for c in tl.cuts}
        last = max(by_epoch, default=-1)
        for f in facts(tl.rows):
            for ep in range(f.epoch, last + 1):
                if ep in by_epoch:
                    count(f, by_epoch[ep], ep - f.epoch, tl.session)
        for cut, now in zip(tl.cuts, open_tasks(tl)):
            for f in now:
                count(f, cut, cut.epoch - f.epoch, tl.session)
    for (s, g), by in per.items():
        res[s][g].sessions = len(by)
        res[s][g].macro = round(sum(h / n for h, n in by.values()) / len(by), 4)
    return res


def delivery(tls: Iterable[Timeline], wake: frozenset[Kind] = WAKES[Wake.HUMAN]) -> dict:
    """Tool calls from an auto compaction to the next `wake` message, as UserPromptSubmit delivery sees it."""
    lags, fate = [], Counter()
    for tl in tls:
        lines = [r.line for r in tl.rows]
        cut_lines = sorted(c.line for c in tl.cuts)
        for c in tl.cuts:
            if c.trigger != Trigger.AUTO:
                continue
            stop = next((x for x in cut_lines if x > c.line), None)
            n, outcome = 0, Fate.EOF
            for r in tl.rows[bisect_right(lines, c.line):]:
                if stop is not None and r.line > stop:
                    outcome = Fate.OVERWRITTEN
                    break
                if r.kind in wake:
                    outcome = Fate.DELIVERED
                    break
                n += r.kind == Kind.TOOL_USE
            fate[outcome] += 1
            if outcome == Fate.DELIVERED:
                lags.append(n)
    return {"auto_cuts": sum(fate.values()), "fate": {k.value: fate[k] for k in Fate},
            "lag_tool_calls": quantiles(lags)}


def coverage(tls: Iterable[Timeline], cutoff: str | None = None,
             budgets: dict[FactKind, int] = BUDGETS) -> dict[Cell, dict[int, list[int]]]:
    """Per generation, [facts visible after a cut, facts]: summary or preserved, alone and with a registry.

    Registry columns render entries as the hook does; FITS counts facts whose cut could list every earlier fact.
    """
    res: dict[Cell, dict[int, list[int]]] = defaultdict(lambda: defaultdict(lambda: [0, 0]))
    for tl in tls:
        every = facts(tl.rows)
        for kind, budget in budgets.items():
            fs = [f for f in every if f.kind == kind]
            for cut in tl.cuts:
                if cut.summary is None:
                    continue
                known = [f for f in fs if f.epoch <= cut.epoch]
                cands = latest(known)
                last = {f.probe: f.row for f in cands}
                missing = [f for f in cands if f.probe not in cut.summary and f.uuid not in cut.preserved]
                chosen = {c: select(missing if lost else cands, budget, p) for c, (p, lost) in REGISTRY.items()}
                fits = sum(map(entry_size, cands)) <= budget
                recents = (False, True) if cutoff and (cut.ts or "") >= cutoff else (False,)
                for f in known:
                    g = cut.epoch - f.epoch
                    if g not in GENS:
                        continue
                    base = f.probe in cut.summary or f.uuid in cut.preserved
                    row = last[f.probe]
                    seen = {Column.SUMMARY: base, Column.FITS: fits} | {
                        c: base or row in rows for c, rows in chosen.items()}
                    for recent in recents:
                        for c, ok in seen.items():
                            cell = res[Cell(c, recent, kind)][g]
                            cell[0] += ok
                            cell[1] += 1
    return res


def _fold(term: str) -> str:
    return "".join(ch for ch in unicodedata.normalize("NFD", term) if unicodedata.category(ch) != "Mn")


def whole_words(text: str, cap: int) -> str:
    """Prefix of at most `cap` chars that does not end inside a word."""
    if len(text) <= cap:
        return text
    cut = text[:cap]
    k = max(cut.rfind(c) for c in " \n\t")
    return cut[:k] if k > 0 else cut


def _queries(f: Fact, df) -> dict[Query, tuple[Index, str]]:
    words = sorted({_fold(w) for w in WORD.findall(f.text.casefold())}, key=lambda w: (df(w) or 1 << 30, w))
    rare = [w for w in words if df(w)][:3]
    out = {Query.PHRASE: (Index.WORDS, quote(whole_words(f.text, PROBE))),
           Query.TRIGRAM: (Index.TRIGRAM, quote(f.text[:30]))}
    if rare:
        out[Query.RARE3] = (Index.WORDS, " ".join(quote(w) for w in rare))
        out[Query.STEM5] = (Index.WORDS, " ".join(quote(w[:5]) + "*" if len(w) > 5 else quote(w) for w in rare))
    return out


def index_check(db: sqlite3.Connection, tls: Iterable[Timeline], limit: int = 1_500) -> dict:
    """Recall@k of requests lost one generation later, queried with their own words: tests the index, not a model."""
    lost = []
    for tl in tls:
        by_epoch = {c.epoch: c for c in tl.cuts}
        for f in facts(tl.rows):
            c = by_epoch.get(f.epoch + 1)
            if f.kind == FactKind.REQUEST and c and c.summary and f.probe not in c.summary and f.uuid not in c.preserved:
                lost.append(f)
    lost.sort(key=lambda f: f.row)
    sample = lost[:: max(1, len(lost) // limit)][:limit]
    db.execute("CREATE VIRTUAL TABLE IF NOT EXISTS temp.vocab USING fts5vocab(main, fts_words, row)")
    cache: dict[str, int] = {}

    def df(term: str) -> int:
        if term not in cache:
            row = db.execute("SELECT doc FROM temp.vocab WHERE term = ?", (term,)).fetchone()
            cache[term] = row[0] if row else 0
        return cache[term]

    hits, totals = Counter(), Counter()
    for f in sample:
        for q, (index, text) in _queries(f, df).items():
            totals[q] += 1
            try:
                found = search(db, text, index, TOP_K, frozenset({Kind.HUMAN}))
            except sqlite3.OperationalError:
                continue
            hits[q] += any(h.id == f.row for h in found)
    return {"lost_after_1": len(lost), "sampled": len(sample), "k": TOP_K,
            "recall": {q.value: [hits[q], totals[q]] for q in Query}}


def quantiles(xs: list[float]) -> Quantiles:
    if not xs:
        return {"n": 0}
    xs = sorted(xs)
    pick = lambda q: xs[min(len(xs) - 1, int(q * len(xs)))]
    return {"n": len(xs), "p10": pick(.1), "p50": pick(.5), "p90": pick(.9), "max": xs[-1]}


def run(db: sqlite3.Connection, tls: list[Timeline], created: str) -> dict:
    cutoff = recent_cutoff(created)
    ret, cov = retention(tls, cutoff), coverage(tls, cutoff=cutoff)
    return {
        "sessions": len(tls),
        "cuts": sum(len(t.cuts) for t in tls),
        "cuts_recent": sum((c.ts or "") >= cutoff for t in tls for c in t.cuts),
        "cuts_without_summary": sum(c.summary is None for t in tls for c in t.cuts),
        "versions": sorted({s.version for s in ret if s.version}, key=version_order),
        "retention": {s.key(): {g: asdict(t) for g, t in sorted(d.items())}
                      for s, d in sorted(ret.items(), key=lambda kv: kv[0].key())},
        "registry": params(),
        "coverage": {c.key(): dict(sorted(d.items())) for c, d in sorted(cov.items(), key=lambda kv: kv[0].key())},
        "delivery": {w.value: delivery(tls, WAKES[w]) for w in Wake},
        "index_check": index_check(db, tls),
    }
