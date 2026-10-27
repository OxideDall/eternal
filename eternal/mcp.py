"""Stdio MCP server: `search` and `zoom` over the archive of every Claude Code session on this machine.

ETERNAL_ARCHIVE pins a frozen archive (read-only, no refresh); ETERNAL_BEFORE hides records at or after an ISO time.
"""
import json
import os
import re
import sqlite3
import sys
import threading
from collections.abc import Callable
from enum import IntEnum, StrEnum
from pathlib import Path
from typing import TypedDict

from . import archive, live, registry
from .archive import Hit, Index, quote
from .kinds import Author, Kind

VERSION = "0.1.0"
PROTOCOL = "2025-06-18"
K_DEFAULT, K_MAX = 10, 30
NEAR_DEFAULT, NEAR_MAX = 3, 20
BRIEF_CAP, BRIEF_MAX = 160, 100  # brief zoom: one short line per neighbour, a timeline
SNIPPET = 300  # chars per search hit
NEAR_CAP = 1_200  # chars per neighbouring record in zoom
FOCUS_CAP = 6_000  # chars of the addressed record
WORD = re.compile(r"\w+")
ADDR = re.compile(r"^\s*([0-9a-f-]{4,36})\s*:\s*L?(\d+)\s*$", re.I)
SESSION = re.compile(r"^[0-9a-f-]{1,36}$", re.I)
HOME = re.compile(r"^-home-[^-]+-?")
PATH = re.compile(r"(?<![\w.~])[~/][^\s,=:'\"()]+")
LONG_PATH = 60
INSTRUCTIONS = ("Archive of every past Claude Code session on this machine: all projects, all compactions. When the "
                "user refers to earlier work that is not in your context, search before answering, then zoom into an "
                "address for the exact text.")


class Method(StrEnum):
    INITIALIZE = "initialize"
    INITIALIZED = "notifications/initialized"
    PING = "ping"
    TOOLS_LIST = "tools/list"
    TOOLS_CALL = "tools/call"


class Tool(StrEnum):
    SEARCH = "search"
    ZOOM = "zoom"


class ContentType(StrEnum):
    TEXT = "text"


class Code(IntEnum):
    PARSE = -32700
    METHOD = -32601
    PARAMS = -32602


class Message(TypedDict, total=False):
    jsonrpc: str
    id: int | str | None
    method: str
    params: dict
    result: dict
    error: dict


class Content(TypedDict):
    type: ContentType
    text: str


class ToolError(Exception):
    """Bad arguments or nothing found; reported to the model as a tool error."""


class RpcError(Exception):
    def __init__(self, code: Code, message: str):
        super().__init__(message)
        self.code = code


TOOLS = [
    {"name": Tool.SEARCH, "description": (
        "Full-text search over past sessions: user prompts, replies, tool calls and results, compaction summaries. "
        "Words match in any order, words of 6+ letters also by prefix (inflections); `exact` matches a substring "
        "such as a path or identifier. Each hit starts with its address, e.g. `ce3cab9e:L81` (session id prefix and "
        "transcript line), then date, author/kind, [project] and snippet."),
     "inputSchema": {"type": "object", "properties": {
         "query": {"type": "string"},
         "exact": {"type": "boolean", "description": "substring match (3+ chars) instead of words"},
         "author": {"type": "string", "enum": [a.value for a in Author]},
         "session": {"type": "string", "description": "session id or its prefix"},
         "since": {"type": "string", "description": "ISO date or time; only records at or after it"},
         "k": {"type": "integer", "minimum": 1, "maximum": K_MAX}},
         "required": ["query"], "additionalProperties": False}},
    {"name": Tool.ZOOM, "description": (
        "Records around an address from search, e.g. `ce3cab9e:L81`; for a registry entry `L81`, prefix the session "
        "named in the registry header. The addressed record in full, neighbours clipped; `brief` turns the "
        "neighbours into a timeline of one short line each, up to 100, to see what happened after something."),
     "inputSchema": {"type": "object", "properties": {
         "address": {"type": "string"},
         "before": {"type": "integer", "minimum": 0, "maximum": BRIEF_MAX},
         "after": {"type": "integer", "minimum": 0, "maximum": BRIEF_MAX, "description": f"up to {NEAR_MAX} unless brief"},
         "brief": {"type": "boolean"},
         "kind": {"type": "string", "enum": [k.value for k in Kind],
                  "description": "neighbours of this kind only, e.g. tool_use for the actions taken"}},
         "required": ["address"], "additionalProperties": False}},
]


def _int(v, default: int, top: int) -> int:
    try:
        return max(0, min(top, int(v)))
    except (TypeError, ValueError):
        return default


def _flat(text: str, cap: int) -> str:
    s = " ".join(text.split())
    return s if len(s) <= cap else s[: cap - 1] + "…"


def _short_path(m: re.Match) -> str:
    p = registry.short(m.group(0))
    return p if len(p) <= LONG_PATH else "…/" + "/".join(p.rstrip("/").split("/")[-2:])


def _brief(text: str, cap: int) -> str:
    """One line with paths shortened so basenames survive the cap."""
    return _flat(PATH.sub(_short_path, " ".join(text.split())), cap)


def terms(query: str) -> list[str]:
    """FTS5 terms: each distinct word quoted, words of 6+ letters as a prefix that tolerates inflected endings."""
    words = [w for w in dict.fromkeys(WORD.findall(query.casefold())) if len(w) > 1]
    return [quote(w[: max(5, len(w) - 2)]) + "*" if len(w) >= 6 else quote(w) for w in words]


def _hit(h: Hit) -> str:
    where = HOME.sub("", h.project) or "~"
    return (f"{h.session[:8]}:L{h.line} {(h.ts or '')[:16].replace('T', ' ')} {h.author}/{h.kind.value} [{where}] "
            f"{_flat(h.snippet, SNIPPET)}")


def search(db: sqlite3.Connection, args: dict, before: str | None, exclude: tuple[str, ...] = ()) -> str:
    q = str(args.get("query") or "").strip()
    if not q:
        raise ToolError("empty query")
    k = _int(args.get("k"), K_DEFAULT, K_MAX) or K_DEFAULT
    try:
        author = Author(args["author"]) if args.get("author") else None
    except ValueError:
        raise ToolError(f"author must be one of {', '.join(a.value for a in Author)}")
    session = str(args.get("session") or "").strip().lower() or None
    if session and not SESSION.match(session):
        raise ToolError("session must be a session id or its hex prefix")
    flt = {"author": author, "session": session, "since": str(args.get("since") or "") or None, "before": before,
           "exclude": exclude}
    ts = terms(q)
    if args.get("exact") or not ts:
        if len(q) < 3:
            raise ToolError("exact search needs at least 3 characters")
        hits, how = archive.search(db, quote(q), Index.TRIGRAM, k, **flt), "substring"
    else:
        hits, how = archive.search(db, " ".join(ts), Index.WORDS, k, **flt), "all words"
        if len(hits) < k and len(ts) > 1:
            seen = {h.id for h in hits}
            more = [h for h in archive.search(db, " OR ".join(ts), Index.WORDS, k, **flt) if h.id not in seen]
            if more:
                hits, how = hits + more[: k - len(hits)], how + ", then any word"
    if not hits:
        return f"no hits for {q!r} ({how})"
    return f"{len(hits)} hits for {q!r} ({how}):\n" + "\n".join(map(_hit, hits))


def zoom(db: sqlite3.Connection, args: dict, before: str | None, exclude: tuple[str, ...] = ()) -> str:
    m = ADDR.match(str(args.get("address") or ""))
    if not m:
        raise ToolError("address must look like ce3cab9e:L81: a session id prefix from a hit, colon, L and line")
    prefix, line = m.group(1).lower(), int(m.group(2))
    srcs = [s for s in db.execute("SELECT id, session, project FROM source WHERE substr(session, 1, ?) = ? LIMIT 6",
                                  (len(prefix), prefix)) if not s[1].startswith(exclude)]
    if not srcs:
        raise ToolError(f"no session {prefix}")
    if len(srcs) > 1:
        raise ToolError("ambiguous session prefix: " + ", ".join(s[1] for s in srcs))
    sid, session, project = srcs[0]
    brief = bool(args.get("brief"))
    top, cap = (BRIEF_MAX, BRIEF_CAP) if brief else (NEAR_MAX, NEAR_CAP)
    nb, na = _int(args.get("before"), NEAR_DEFAULT, top), _int(args.get("after"), NEAR_DEFAULT, top)
    try:
        kind = Kind(args["kind"]) if args.get("kind") else None
    except ValueError:
        raise ToolError(f"kind must be one of {', '.join(k.value for k in Kind)}")
    hide, extra = (" AND ts < ?", [before]) if before else ("", [])
    near, near_extra = (hide + " AND kind = ?", [*extra, kind.value]) if kind else (hide, extra)
    cols = "SELECT line, block, kind, author, ts, text FROM record WHERE source_id = ?"
    focus = db.execute(f"{cols} AND line >= ?{hide} ORDER BY line, block LIMIT 1", (sid, line, *extra)).fetchone()
    if not focus:
        raise ToolError(f"nothing at or after line {line} in {session[:8]}")
    prev = db.execute(f"{cols} AND (line, block) < (?, ?){near} ORDER BY line DESC, block DESC LIMIT ?",
                      (sid, focus[0], focus[1], *near_extra, nb)).fetchall()[::-1]
    nxt = db.execute(f"{cols} AND (line, block) > (?, ?){near} ORDER BY line, block LIMIT ?",
                     (sid, focus[0], focus[1], *near_extra, na)).fetchall()
    clip = _brief if brief else _flat
    show = lambda r, mark, cap: f"{mark} L{r[0]} {(r[4] or '')[:16].replace('T', ' ')} {r[3]}/{r[2]}: {clip(r[5], cap)}"
    out = [f"session {session} [{HOME.sub('', project) or '~'}]"]
    out += [show(r, " ", cap) for r in prev]
    out.append(f"> L{focus[0]} {(focus[4] or '')[:16].replace('T', ' ')} {focus[3]}/{focus[2]}: {_flat(focus[5], FOCUS_CAP)}")
    out += [show(r, " ", cap) for r in nxt]
    return "\n".join(out)


TOOL_FNS: dict[Tool, Callable[..., str]] = {Tool.SEARCH: search, Tool.ZOOM: zoom}


class Server:
    def __init__(self, env: dict[str, str] | None = None):
        env = os.environ if env is None else env
        self.before = env.get("ETERNAL_BEFORE") or None
        self.exclude = tuple(x for x in (y.strip().lower() for y in env.get("ETERNAL_EXCLUDE", "").split(","))
                             if SESSION.match(x))
        pinned = env.get("ETERNAL_ARCHIVE")
        self.frozen = bool(pinned)
        if pinned:
            self.db = sqlite3.connect(f"file:{Path(pinned).expanduser()}?mode=ro", uri=True)
        else:
            archive.check_space(live.db_path(), 1 << 30)
            self.db = archive.connect(live.db_path())

    def warm(self) -> None:
        """Catch up the live archive in the background so the first call does not wait for a full ingest."""
        if self.frozen:
            return

        def run() -> None:
            db = archive.connect(live.db_path())
            try:
                live.refresh(db, block=True)
            except Exception as e:
                print(f"eternal: warm refresh failed: {e}", file=sys.stderr)
            finally:
                db.close()
        threading.Thread(target=run, daemon=True).start()

    def call(self, params: dict) -> dict:
        try:
            tool = Tool(params.get("name"))
        except ValueError:
            raise RpcError(Code.PARAMS, f"unknown tool {params.get('name')!r}")
        if not self.frozen:
            try:
                live.refresh(self.db, block=False)
            except Exception as e:  # search the archive as it is
                print(f"eternal: refresh failed: {e}", file=sys.stderr)
        exclude = self.exclude if self.frozen else self.exclude + tuple(x for x in live.excluded() if SESSION.match(x))
        try:
            text, bad = TOOL_FNS[tool](self.db, params.get("arguments") or {}, self.before, exclude), False
        except (ToolError, sqlite3.Error) as e:
            text, bad = f"error: {e}", True
        return {"content": [Content(type=ContentType.TEXT, text=text)], "isError": bad}

    def handle(self, msg: Message) -> Message | None:
        rid = msg.get("id")
        try:
            method = Method(msg.get("method"))
        except ValueError:
            return None if rid is None else _error(rid, Code.METHOD, f"unknown method {msg.get('method')!r}")
        if rid is None:
            return None
        params = msg.get("params") or {}
        try:
            if method == Method.INITIALIZE:
                result = {"protocolVersion": params.get("protocolVersion") or PROTOCOL, "capabilities": {"tools": {}},
                          "serverInfo": {"name": "eternal", "version": VERSION}, "instructions": INSTRUCTIONS}
            elif method == Method.TOOLS_LIST:
                result = {"tools": TOOLS}
            elif method == Method.TOOLS_CALL:
                result = self.call(params)
            else:
                result = {}
        except RpcError as e:
            return _error(rid, e.code, str(e))
        return {"jsonrpc": "2.0", "id": rid, "result": result}


def _error(rid, code: Code, message: str) -> Message:
    return {"jsonrpc": "2.0", "id": rid, "error": {"code": int(code), "message": message}}


def main() -> None:
    server = Server()
    server.warm()
    for line in sys.stdin:
        if not line.strip():
            continue
        try:
            msg = json.loads(line)
            reply = server.handle(msg) if isinstance(msg, dict) else _error(None, Code.PARSE, "not an object")
        except ValueError:
            reply = _error(None, Code.PARSE, "invalid JSON")
        if reply is not None:
            sys.stdout.write(json.dumps(reply, ensure_ascii=False) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
