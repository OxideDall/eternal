"""Append-only SQLite archive of transcripts with code-assigned provenance and FTS5 search."""
import json
import os
import re
import shutil
import sqlite3
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

from . import corpus
from .classify import classify
from .kinds import AUTHOR, INDEXED_KINDS, Author, Boundary, Kind, Record, Request, Snapshot

MIN_FREE = 8 << 30
SELF_TOOLS = "mcp__eternal__"  # stored but not indexed, or each search would find the earlier ones

SCHEMA = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS source (
  id INTEGER PRIMARY KEY, path TEXT UNIQUE NOT NULL, session TEXT NOT NULL, project TEXT NOT NULL,
  sub INTEGER NOT NULL, bytes INTEGER NOT NULL DEFAULT 0, lines INTEGER NOT NULL DEFAULT 0,
  epoch INTEGER NOT NULL DEFAULT 0, sha256 TEXT NOT NULL DEFAULT '');
CREATE TABLE IF NOT EXISTS record (
  id INTEGER PRIMARY KEY, source_id INTEGER NOT NULL, line INTEGER NOT NULL, block INTEGER NOT NULL,
  kind TEXT NOT NULL, author TEXT NOT NULL, epoch INTEGER NOT NULL, uuid TEXT, ts TEXT, tool TEXT,
  tool_use_id TEXT, args TEXT, via TEXT, chars INTEGER NOT NULL, text TEXT NOT NULL,
  UNIQUE (source_id, line, block));
CREATE INDEX IF NOT EXISTS record_source ON record(source_id, line);
CREATE TABLE IF NOT EXISTS boundary (
  id INTEGER PRIMARY KEY, source_id INTEGER NOT NULL, line INTEGER NOT NULL, epoch INTEGER NOT NULL,
  uuid TEXT, ts TEXT, version TEXT, trigger TEXT NOT NULL, pre_tokens INTEGER, post_tokens INTEGER,
  preserved TEXT NOT NULL, summary_id INTEGER, UNIQUE (source_id, line));
CREATE TABLE IF NOT EXISTS request (
  id INTEGER PRIMARY KEY, source_id INTEGER NOT NULL, line INTEGER NOT NULL, epoch INTEGER NOT NULL,
  msg_id TEXT NOT NULL, ts TEXT, model TEXT, version TEXT, input INTEGER NOT NULL,
  cache_write INTEGER NOT NULL, cache_read INTEGER NOT NULL, output INTEGER NOT NULL,
  UNIQUE (source_id, msg_id));
CREATE TABLE IF NOT EXISTS snapshot (
  source_id INTEGER NOT NULL, line INTEGER NOT NULL, ts TEXT, system_chars INTEGER NOT NULL,
  tools_chars INTEGER NOT NULL, tools INTEGER NOT NULL, PRIMARY KEY (source_id, line));
CREATE VIRTUAL TABLE IF NOT EXISTS fts_words USING fts5(text, content='record', content_rowid='id',
  tokenize='unicode61 remove_diacritics 2');
CREATE VIRTUAL TABLE IF NOT EXISTS fts_tri USING fts5(text, content='record', content_rowid='id',
  tokenize='trigram');
"""


class Index(StrEnum):
    WORDS = "fts_words"
    TRIGRAM = "fts_tri"


SECRETS: tuple[tuple[str, re.Pattern], ...] = (
    ("pem", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?(?:-----END [A-Z ]*PRIVATE KEY-----|$)", re.S)),
    ("anthropic", re.compile(r"sk-ant-[A-Za-z0-9_\-]{16,}")),
    ("openai", re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_\-]{32,}")),
    ("github", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}")),
    ("slack", re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}")),
    ("aws", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("google", re.compile(r"\bAIza[0-9A-Za-z_\-]{35}")),
    ("tailscale", re.compile(r"\btskey-[A-Za-z0-9-]{10,}")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}")),
    ("bearer", re.compile(r"(?i)(\bbearer\s+)[A-Za-z0-9._~+/\-]{20,}=*")),
    ("assign", re.compile(r"(?i)(\b(?:api[_-]?key|secret|token|password|passwd)\b\s*[:=]\s*['\"]?)[^\s'\"]{8,}")),
)


def redact(text: str, counts: Counter) -> str:
    """Mask common secret shapes; a mitigation, not a guarantee."""
    for name, rx in SECRETS:
        def sub(m: re.Match, name=name) -> str:
            counts[name] += 1
            keep = m.group(1) if rx.groups else ""
            return f"{keep}[redacted:{name}]"
        text = rx.sub(sub, text)
    return text


@dataclass
class Stats:
    files: int = 0
    lines: int = 0
    bad_json: int = 0
    kinds: Counter = field(default_factory=Counter)
    boundaries: int = 0
    unsummarized: int = 0
    requests: int = 0
    redacted: Counter = field(default_factory=Counter)
    skipped: list[str] = field(default_factory=list)  # files whose ingested prefix changed (live archives only)


def connect(path: Path) -> sqlite3.Connection:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    db = sqlite3.connect(path, timeout=30)
    db.executescript(SCHEMA)
    os.chmod(path, 0o600)
    return db


def check_space(path: Path, min_free: int = MIN_FREE) -> None:
    """Exit unless the volume that will hold `path` has `min_free` bytes; the directory may not exist yet."""
    d = Path(path).parent
    while not d.exists():
        d = d.parent
    free = shutil.disk_usage(d).free
    if free < min_free:
        raise SystemExit(f"{d}: {free >> 30} GiB free < {min_free >> 30} GiB; set ETERNAL_DATA elsewhere")


def _source(db: sqlite3.Connection, f: corpus.Frozen) -> tuple[int, int, int, int]:
    db.execute("INSERT OR IGNORE INTO source(path, session, project, sub) VALUES (?, ?, ?, ?)",
               (f.path, f.session, f.project, int(f.sub)))
    return db.execute("SELECT id, bytes, lines, epoch FROM source WHERE path = ?", (f.path,)).fetchone()


def ingest(db: sqlite3.Connection, m: corpus.Manifest, progress: Callable[[str], None] | None = None,
           digest: corpus.Digest = corpus.Digest.PREFIX, skip_changed: bool = False) -> Stats:
    """Append each frozen file's unseen suffix; a file whose ingested prefix changed raises, or is skipped."""
    held = dict(db.execute("SELECT key, value FROM meta")).get("digest")
    if held is None and db.execute("SELECT 1 FROM source WHERE bytes > 0 LIMIT 1").fetchone():
        held = corpus.Digest.PREFIX  # archives made before the digest was recorded
    if held is not None and held != digest:
        raise ValueError(f"archive fingerprints prefixes by {held}, not {digest}")
    db.execute("INSERT OR IGNORE INTO meta VALUES ('digest', ?)", (digest.value,))
    db.commit()
    st = Stats()
    for f in m.files:
        sid, done, nline, epoch = _source(db, f)
        if done == f.size:
            continue
        if done > f.size or (done and corpus.DIGESTS[digest](Path(m.root) / f.path, done) != db.execute(
                "SELECT sha256 FROM source WHERE id = ?", (sid,)).fetchone()[0]):
            if skip_changed:
                st.skipped.append(f.path)
                continue
            raise ValueError(f"{f.path}: ingested prefix of {done} bytes changed; drop the source and re-ingest")
        pending = db.execute("SELECT id FROM boundary WHERE source_id = ? AND summary_id IS NULL "
                             "ORDER BY line DESC LIMIT 1", (sid,)).fetchone()
        pending = pending[0] if pending else None
        own = {u for (u,) in db.execute("SELECT tool_use_id FROM record WHERE source_id = ? AND kind = ? "
                                         "AND substr(tool, 1, ?) = ?",
                                         (sid, Kind.TOOL_USE.value, len(SELF_TOOLS), SELF_TOOLS))}
        for _, raw in corpus.lines(m, f, done):
            line, nline = nline, nline + 1
            st.lines += 1
            try:
                e = json.loads(raw)
            except ValueError:
                st.bad_json += 1
                continue
            if not isinstance(e, dict):
                continue
            for ev in classify(e, line):
                if isinstance(ev, Record):
                    if ev.kind == Kind.TOOL_USE and (ev.tool or "").startswith(SELF_TOOLS) and ev.tool_use_id:
                        own.add(ev.tool_use_id)
                    rid = _put_record(db, sid, epoch, ev, st, index=ev.tool_use_id not in own)
                    if ev.kind == Kind.COMPACT_SUMMARY and pending is not None:
                        db.execute("UPDATE boundary SET summary_id = ? WHERE id = ?", (rid, pending))
                        pending = None
                elif isinstance(ev, Boundary):
                    st.unsummarized += pending is not None
                    cur = db.execute(
                        "INSERT INTO boundary(source_id, line, epoch, uuid, ts, version, trigger, pre_tokens,"
                        " post_tokens, preserved) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (sid, ev.line, epoch, ev.uuid, ev.ts, ev.version, ev.trigger.value, ev.pre_tokens,
                         ev.post_tokens, json.dumps(ev.preserved)))
                    pending, epoch = cur.lastrowid, epoch + 1
                    st.boundaries += 1
                elif isinstance(ev, Request):
                    st.requests += 1
                    db.execute(
                        "INSERT INTO request(source_id, line, epoch, msg_id, ts, model, version, input, cache_write,"
                        " cache_read, output) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
                        " ON CONFLICT(source_id, msg_id) DO UPDATE SET output = max(output, excluded.output)",
                        (sid, ev.line, epoch, ev.msg_id, ev.ts, ev.model, ev.version, ev.input, ev.cache_write,
                         ev.cache_read, ev.output))
                elif isinstance(ev, Snapshot):
                    db.execute("INSERT OR IGNORE INTO snapshot VALUES (?, ?, ?, ?, ?, ?)",
                               (sid, ev.line, ev.ts, ev.system_chars, ev.tools_chars, ev.tools))
        db.execute("UPDATE source SET bytes = ?, lines = ?, epoch = ?, sha256 = ? WHERE id = ?",
                   (f.size, nline, epoch, f.sha256, sid))
        db.commit()
        st.files += 1
        if progress:
            progress(f.path)
    return st


def _put_record(db: sqlite3.Connection, sid: int, epoch: int, r: Record, st: Stats, index: bool = True) -> int:
    text = redact(r.text, st.redacted)
    args = {k: redact(v, st.redacted) for k, v in r.args.items()} if r.args else None
    cur = db.execute(
        "INSERT INTO record(source_id, line, block, kind, author, epoch, uuid, ts, tool, tool_use_id, args, via,"
        " chars, text) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (sid, r.line, r.block, r.kind.value, AUTHOR[r.kind].value, epoch, r.uuid, r.ts, r.tool, r.tool_use_id,
         json.dumps(args, ensure_ascii=False) if args else None, r.via.value if r.via else None, r.chars, text))
    st.kinds[r.kind] += 1
    if index and r.kind in INDEXED_KINDS:
        for ix in Index:
            db.execute(f"INSERT INTO {ix.value}(rowid, text) VALUES (?, ?)", (cur.lastrowid, text))
    return cur.lastrowid


@dataclass(frozen=True, slots=True)
class Hit:
    id: int
    session: str
    ts: str | None
    kind: Kind
    author: Author
    snippet: str
    line: int = 0
    project: str = ""


def quote(term: str) -> str:
    return '"' + term.replace('"', '""') + '"'


def search(db: sqlite3.Connection, query: str, index: Index = Index.WORDS, k: int = 10,
           kinds: frozenset[Kind] | None = None, author: Author | None = None, session: str | None = None,
           since: str | None = None, before: str | None = None, exclude: tuple[str, ...] = ()) -> list[Hit]:
    """BM25-ranked hits for an FTS5 query; caller quotes user text with `quote`.

    `before` drops records at or after that time, `exclude` sessions starting with any of the prefixes.
    """
    sql = (f"SELECT r.id, s.session, r.ts, r.kind, r.author, snippet({index.value}, 0, '[', ']', '…', 12), r.line, "
           f"s.project FROM {index.value} JOIN record r ON r.id = {index.value}.rowid "
           f"JOIN source s ON s.id = r.source_id WHERE {index.value} MATCH ?")
    params: list = [query]
    if kinds:
        sql += f" AND r.kind IN ({','.join('?' * len(kinds))})"
        params += [x.value for x in kinds]
    for cond, value in (("r.author = ?", author.value if author else None), ("s.session LIKE ? || '%'", session),
                        ("r.ts >= ?", since), ("r.ts < ?", before)):
        if value:
            sql += f" AND {cond}"
            params.append(value)
    for x in exclude:
        sql += " AND substr(s.session, 1, ?) != ?"
        params += [len(x), x]
    sql += f" ORDER BY bm25({index.value}) LIMIT ?"
    params.append(k)
    return [Hit(i, s, ts, Kind(kd), Author(a), sn, ln, pr) for i, s, ts, kd, a, sn, ln, pr in db.execute(sql, params)]
