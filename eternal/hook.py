"""Claude Code SessionStart hook: after a compaction, inject the session's registry; log every firing."""
import hashlib
import json
import os
import sys
import time
from collections import Counter
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timezone
from enum import StrEnum
from functools import cache
from pathlib import Path
from typing import TypedDict

from . import registry
from .archive import redact
from .classify import WRITE_TOOLS, classify
from .corpus import tail_sha
from .facts import FactKind, Row
from .kinds import Boundary, Kind, Record, Via
from .live import live_dir

CODE = ("archive", "classify", "facts", "kinds", "hook")  # modules that shape a scan; editing one voids the caches


class HookEvent(StrEnum):
    SESSION_START = "SessionStart"


class StartSource(StrEnum):
    STARTUP = "startup"
    RESUME = "resume"
    CLEAR = "clear"
    COMPACT = "compact"


class HookInput(TypedDict, total=False):
    session_id: str
    transcript_path: str
    cwd: str
    hook_event_name: str
    source: str


class Firing(TypedDict, total=False):
    ts: str
    session: str
    event: str
    source: str
    bytes: int
    lines: int
    boundaries: int
    summarized: bool  # the last boundary already in the transcript has its summary
    prompts: int
    files: int
    shown_prompts: int
    shown_files: int
    reused_lines: int  # transcript lines taken from the scan cache instead of parsed again
    code: str  # code_digest() of the hook that fired
    chars: int
    seconds: float
    error: str


@dataclass
class Scan:
    rows: list[Row] = field(default_factory=list)
    lines: int = 0
    boundaries: int = 0  # also the epoch of the next record
    summarized: bool = True
    offset: int = 0  # bytes through the last complete line


def scan(path: Path, start: Scan | None = None) -> Scan:
    """Prompts and file writes of a live transcript, numbered as the archive numbers lines; stops at a partial line.

    `start` is an earlier scan of this file's unchanged prefix to continue from.
    """
    st, counts = Scan() if start is None else replace(start, rows=list(start.rows)), Counter()
    with open(path, "rb") as f:
        f.seek(st.offset)
        for line, raw in enumerate(f, st.lines):
            if not raw.endswith(b"\n"):
                break
            st.lines, st.offset = line + 1, st.offset + len(raw)
            try:
                e = json.loads(raw)
            except ValueError:
                continue
            if not isinstance(e, dict):
                continue
            for ev in classify(e, line):
                if isinstance(ev, Boundary):
                    st.boundaries += 1
                    st.summarized = False
                elif not isinstance(ev, Record):
                    continue
                elif ev.kind == Kind.COMPACT_SUMMARY:
                    st.summarized = True
                elif ev.kind == Kind.HUMAN or (ev.kind == Kind.TOOL_USE and ev.tool in WRITE_TOOLS):
                    args = {k: redact(v, counts) for k, v in ev.args.items()} if ev.args else None
                    st.rows.append(Row(len(st.rows), line, ev.kind, st.boundaries, ev.uuid, ev.ts, ev.tool,
                                       ev.tool_use_id, args, redact(ev.text, counts), ev.via))
    return st


@cache
def code_digest() -> str:
    here = Path(__file__).parent
    return hashlib.sha256(b"".join((here / f"{m}.py").read_bytes() for m in CODE)).hexdigest()[:16]


def cache_file(live: Path, path: Path) -> Path:
    return live / "scan" / (hashlib.sha256(str(path).encode()).hexdigest()[:16] + ".json")


def load_scan(cache: Path, path: Path) -> Scan | None:
    """The cached scan of `path` if the code and the transcript's size and tail at the offset match, as live checks."""
    try:
        d = json.loads(cache.read_text())
        s = d["scan"]
        if d["code"] != code_digest() or d["path"] != str(path) or tail_sha(path, s["offset"]) != d["tail"]:
            return None
        rows = [Row(**(r | {"kind": Kind(r["kind"]), "via": Via(r["via"]) if r["via"] else None})) for r in s["rows"]]
        return Scan(rows, s["lines"], s["boundaries"], s["summarized"], s["offset"])
    except (OSError, ValueError, KeyError, TypeError):
        return None


def save_scan(cache: Path, path: Path, st: Scan) -> None:
    """Atomic, so a hook killed mid-write leaves the previous cache."""
    cache.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    body = json.dumps({"code": code_digest(), "path": str(path), "tail": tail_sha(path, st.offset),
                       "scan": asdict(st)}, ensure_ascii=False)
    tmp = cache.with_name(f"{cache.name}.{os.getpid()}.tmp")
    with os.fdopen(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as f:
        f.write(body)
    os.replace(tmp, cache)


def scan_cached(path: Path, live: Path) -> tuple[Scan, int]:
    """The scan continued from the cache, and how many lines it reused; the cache is refreshed."""
    cache = cache_file(live, path)
    start = load_scan(cache, path)
    st = scan(path, start)
    try:
        save_scan(cache, path, st)
    except OSError:
        pass
    return st, start.lines if start else 0


def handle(inp: HookInput, live: Path | None = None) -> tuple[dict | None, Firing]:
    """Hook output (None for no injection) and the log record; never raises. `live` holds the scan cache."""
    t0 = time.monotonic()
    rec: Firing = {"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                   "session": str(inp.get("session_id", ""))[:8], "event": str(inp.get("hook_event_name", "")),
                   "source": str(inp.get("source", ""))}
    out = None
    try:
        rec["code"] = code_digest()
        event, source = HookEvent(inp.get("hook_event_name")), StartSource(inp.get("source"))
        path = Path(inp["transcript_path"])
        if source != StartSource.COMPACT:
            rec["bytes"] = path.stat().st_size if path.exists() else 0  # no transcript yet on startup
        else:
            rec["bytes"] = path.stat().st_size
            st, rec["reused_lines"] = scan_cached(path, live or live_dir())
            b = registry.build(st.rows, str(inp.get("session_id") or path.stem))
            rec |= {"lines": st.lines, "boundaries": st.boundaries, "summarized": st.summarized,
                    "prompts": b.totals[FactKind.REQUEST], "files": b.totals[FactKind.FILE],
                    "shown_prompts": b.shown[FactKind.REQUEST], "shown_files": b.shown[FactKind.FILE]}
            if any(b.shown.values()):
                rec["chars"] = len(b.text)
                out = {"hookSpecificOutput": {"hookEventName": event.value, "additionalContext": b.text}}
    except Exception as e:  # a hook must not break the session
        rec["error"] = f"{type(e).__name__}: {e}"[:300]
    rec["seconds"] = round(time.monotonic() - t0, 3)
    return out, rec


class Delivery(StrEnum):
    DELIVERED = "delivered"  # registry attached after the boundary, before the next model request
    LATE = "late"  # attached only after that request
    MISSING = "missing"  # a request followed without a registry
    PENDING = "pending"  # no request after the boundary yet


MARK = "[eternal registry]"


def _is_registry(a: dict) -> bool:
    c = a.get("content")
    return (a.get("type") == "hook_additional_context" and a.get("hookEvent") == HookEvent.SESSION_START
            and isinstance(c, list) and any(isinstance(x, str) and x.startswith(MARK) for x in c))


def audit(path: Path) -> list[Delivery]:
    """Per compaction boundary of a transcript, whether the registry reached the next model request."""
    out: list[Delivery] = []
    cur: list[bool] | None = None  # [attached before a request, request seen, attached after it]

    def close() -> None:
        if cur is not None:
            out.append(Delivery.DELIVERED if cur[0] else Delivery.PENDING if not cur[1]
                       else Delivery.LATE if cur[2] else Delivery.MISSING)

    with open(path, "rb") as f:
        for raw in f:
            try:
                e = json.loads(raw)
            except ValueError:
                continue
            if not isinstance(e, dict):
                continue
            t = e.get("type")
            if t == "system" and e.get("subtype") == "compact_boundary":
                close()
                cur = [False, False, False]
            elif cur is None:
                continue
            elif t == "attachment" and isinstance(e.get("attachment"), dict) and _is_registry(e["attachment"]):
                cur[2 if cur[1] else 0] = True
            elif t == "assistant" and (e.get("message") or {}).get("usage") and e["message"].get("model") != "<synthetic>":
                cur[1] = True
    close()
    return out


def precomputed(path: Path) -> int:
    """Compaction boundaries of a transcript whose summary Claude Code computed ahead and swapped in."""
    n = 0
    with open(path, "rb") as f:
        for raw in f:
            if b"compact_boundary" not in raw:
                continue
            try:
                e = json.loads(raw)
            except ValueError:
                continue
            n += (isinstance(e, dict) and e.get("subtype") == "compact_boundary"
                  and bool((e.get("compactMetadata") or {}).get("precomputed")))
    return n


def log(rec: Firing, live: Path) -> None:
    try:
        live.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd = os.open(live / "hook.jsonl", os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        with os.fdopen(fd, "a") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except OSError:
        pass


def main() -> None:
    try:
        inp = json.load(sys.stdin)
    except ValueError:
        inp = {}
    live = live_dir()
    out, rec = handle(inp if isinstance(inp, dict) else {}, live)
    log(rec, live)
    if out:
        print(json.dumps(out, ensure_ascii=False))


if __name__ == "__main__":
    main()
