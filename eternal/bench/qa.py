"""B5 (Tier-2): after later compactions, ask which file was written first after an old prompt; S0–S4 compared."""
import hashlib
import itertools
import json
import os
import re
import shutil
import sqlite3
import subprocess
import tempfile
import time
from collections import Counter, defaultdict
from collections.abc import Iterable
from contextlib import closing
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import TypedDict

from .. import mcp, registry
from ..facts import MIN_PROMPT, FactKind, own_words, written
from ..kinds import Kind
from .replay import whole_words
from .timeline import Timeline, load

WRAPPER = Path.home() / "projects/claude_tune/claude-wrapped"
REPO = Path(__file__).resolve().parents[2]
GENS = (1, 3, 10)
QUOTE = 120  # chars of the old prompt quoted in a question
UNCLEAR = "UNCLEAR"
GENERIC = frozenset({
    "readme.md", "__init__.py", "settings.json", "settings.local.json", "claude.md", "memory.md", "package.json",
    "main.py", "index.ts", "index.js", "config.json", "test.py", "notes.md", "todo.md", "plan.md", ".gitignore",
    "makefile", "dockerfile", "pyproject.toml", "requirements.txt", "cargo.toml", "go.mod"})
FILE_TOKEN = re.compile(r"[\w\-]+(?:\.[\w\-]+)*\.[A-Za-z0-9]{1,10}")
HIT_ADDR = re.compile(r"^([0-9a-f]{8}):L(\d+) ", re.M)
ENTRY_LINE = re.compile(r"L(\d+) ")
HOOK_WRAP = "<system-reminder>\nSessionStart:compact hook additional context: {}\n</system-reminder>"
TOOLS = "mcp__eternal__search,mcp__eternal__zoom"
FILE_TOOLS = "Read,Grep"
FOOTER = re.compile(r"(read the full transcript at: )(\S+\.jsonl)")
SANDBOX = Path("/var/tmp")  # each call runs in a fresh dir here, away from run logs, archives and transcripts
MAX_FAILS = 3  # consecutive failures outside the model that stop a run


class System(StrEnum):
    S0 = "S0"  # the compaction summary
    S1 = "S1"  # summary + registry, as the SessionStart hook injects it
    S2 = "S2"  # S1 + search and zoom over the archive before the cut
    S3 = "S3"  # summary + search and zoom, no registry: what search gives alone
    S4 = "S4"  # summary + Read/Grep on the transcript cut at the boundary: plain Claude Code's own recall


class Effort(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    XHIGH = "xhigh"
    MAX = "max"


REGISTRY = frozenset({System.S1, System.S2})
SEARCH = frozenset({System.S2, System.S3})
TOOLED = SEARCH | {System.S4}
TURNS = 8  # default turn cap of the tooled systems; another cap shows in the system's label, e.g. S4@20
EFFORT = Effort.LOW  # default effort; another one shows in the label, e.g. S4@20/max
TIMEOUT = 600  # seconds per call; a timeout counts as a failure outside the model and is retried


class Stratum(StrEnum):
    ALL = "all"
    SHOWN = "shown"  # the registry at the cut lists the prompt and its files, so S1 can answer by construction
    HIDDEN = "hidden"  # thinned out of the registry: only the summary or search can answer


class Verdict(StrEnum):
    CORRECT = "correct"
    WRONG = "wrong"
    UNCLEAR = "unclear"


@dataclass(frozen=True, slots=True)
class Question:
    session: str
    project: str
    source_id: int
    epoch: int
    line: int  # transcript line of the old prompt
    prompt: str
    golden: str  # basename of the first file written after the prompt
    path: str
    distance: int  # records from the prompt to the write
    generation: int
    cut_line: int
    cut_ts: str
    version: str | None

    @property
    def key(self) -> str:
        return f"{self.session[:8]}-L{self.line}-g{self.generation}"

    @property
    def quote(self) -> str:
        """The prompt's start cut at a word boundary, as quoted to the model and used as the ceiling query."""
        flat = " ".join(self.prompt.split())
        cut = whole_words(flat, QUOTE)
        return cut if cut == flat else cut + "…"

    def text(self) -> str:
        return ("Вопрос о более ранней части этой сессии. Пользователь написал промпт, который начинается так:\n"
                f"«{self.quote}»\n"
                "Какой файл был записан (Write или Edit) первым после этого промпта, до следующего промпта "
                f"пользователя? Ответь одной строкой: только имя файла без пути. Если не знаешь, ответь {UNCLEAR}.")


class Call(TypedDict, total=False):
    key: str
    system: str
    generation: int
    golden: str
    answer: str
    verdict: str
    model: str
    cost_usd: float
    turns: int
    seconds: float
    tool_calls: list[str]
    usage: dict
    limits: dict
    error: str
    shown: bool
    registry: dict  # registry.params() at run time
    argv: list[str]
    denied: list[str]  # paths of refused tool calls; such calls are kept out of the score


def questions(tls: Iterable[Timeline], gens: tuple[int, ...] = GENS, since: str | None = None,
              exclude: tuple[str, ...] = ()) -> list[Question]:
    """Every old prompt whose next write, before the next prompt, has an unambiguous basename it does not name."""
    out = []
    for tl in tls:
        if tl.session.startswith(exclude):
            continue
        cuts = {c.epoch: c for c in tl.cuts}
        paths = defaultdict(set)
        for r in tl.rows:
            if p := written(r):
                paths[r.epoch, os.path.basename(p).casefold()].add(p)
        for i, r in enumerate(tl.rows):
            words = own_words(r.text) if r.kind == Kind.HUMAN else ""
            if len(words) < MIN_PROMPT:
                continue
            path, dist = None, 0
            for dist, nxt in enumerate(tl.rows[i + 1:], 1):
                if nxt.epoch != r.epoch or nxt.kind == Kind.HUMAN:
                    break
                if path := written(nxt):
                    break
            if not path:
                continue
            base = os.path.basename(path)
            fold, said = base.casefold(), words.casefold()
            stem = os.path.splitext(fold)[0]
            if fold in GENERIC or len(paths[r.epoch, fold]) > 1 or fold in said or (len(stem) >= 4 and stem in said):
                continue
            for g in gens:
                c = cuts.get(r.epoch + g)
                if c and c.summary and c.ts and (not since or c.ts >= since):
                    out.append(Question(tl.session, tl.project, tl.source_id, r.epoch, r.line, words, base,
                                        path, dist, g, c.line, c.ts, c.version))
    return out


def sample(qs: list[Question], per_gen: dict[int, int], seed: str = "b5") -> list[Question]:
    """Deterministic pick of `per_gen[g]` questions per generation, one per session first."""
    order = sorted(qs, key=lambda q: hashlib.sha256(f"{seed}:{q.key}".encode()).hexdigest())
    out, taken, used = [], Counter(), set()
    for strict in (True, False):
        for q in order:
            room = taken[q.generation] < per_gen.get(q.generation, 0)
            if room and q not in out and not (strict and q.session in used):
                out.append(q)
                taken[q.generation] += 1
                used.add(q.session)
    return sorted(out, key=lambda q: (q.generation, q.key))


def verdict(answer: str, golden: str) -> Verdict:
    """Correct only when the answer names exactly one file and it is the golden basename."""
    a = answer.strip().casefold()
    if not a or UNCLEAR.casefold() in a:
        return Verdict.UNCLEAR
    g = golden.casefold()
    names = {os.path.basename(t) for t in FILE_TOKEN.findall(a)}
    if names:
        return Verdict.CORRECT if names == {g} else Verdict.WRONG
    return Verdict.CORRECT if re.search(rf"(?<![\w.\-]){re.escape(g)}(?![\w.\-])", a) else Verdict.WRONG


def summary_text(db: sqlite3.Connection, q: Question) -> str:
    return db.execute("SELECT r.text FROM boundary b JOIN record r ON r.id = b.summary_id "
                      "WHERE b.source_id = ? AND b.line = ?", (q.source_id, q.cut_line)).fetchone()[0]


def registry_text(tl: Timeline, q: Question) -> str:
    return registry.build([r for r in tl.rows if r.line < q.cut_line], tl.session).text


def listed(reg: str, line: int) -> bool:
    """Whether the rendered registry's prompt section lists the prompt at transcript `line`."""
    inside = False
    for x in reg.splitlines():
        if m := ENTRY_LINE.match(x):
            if inside and int(m.group(1)) == line:
                return True
        else:
            inside = x.startswith(f"{registry.TITLES[FactKind.REQUEST]} (")
    return False


def strata(tls: Iterable[Timeline], qs: list[Question]) -> dict[Question, Stratum]:
    """Each question's stratum under the current registry format, one render per cut."""
    by = {tl.source_id: tl for tl in tls}
    regs: dict[tuple[int, int], str] = {}
    out = {}
    for q in qs:
        k = (q.source_id, q.cut_line)
        if k not in regs:
            regs[k] = registry_text(by[q.source_id], q)
        out[q] = Stratum.SHOWN if listed(regs[k], q.line) else Stratum.HIDDEN
    return out


def prompt(system: System, summary: str, reg: str, q: Question) -> str:
    parts = [summary] + ([HOOK_WRAP.format(reg)] if system in REGISTRY else []) + [q.text()]
    return "\n\n".join(parts)


def ceiling(db: sqlite3.Connection, qs: list[Question], k: int = 10, after: int = mcp.BRIEF_MAX,
            exclude: tuple[str, ...] = ()) -> dict:
    """Free upper bound for S2: the prompt found by its own words, the golden file in a brief zoom after it."""
    per: dict[int, Counter] = defaultdict(Counter)
    leaks = 0
    for q in qs:
        c = per[q.generation]
        c["questions"] += 1
        text = mcp.search(db, {"query": q.quote.rstrip("…"), "k": k}, q.cut_ts, exclude)
        addrs = [(s, int(n)) for s, n in HIT_ADDR.findall(text)]
        for s, n in addrs:
            ts = db.execute("SELECT max(r.ts) FROM record r JOIN source o ON o.id = r.source_id "
                            "WHERE substr(o.session, 1, 8) = ? AND r.line = ?", (s, n)).fetchone()[0]
            leaks += bool(ts and ts >= q.cut_ts)
        if (q.session[:8], q.line) in addrs:
            c["prompt_found"] += 1
            for metric, kind in (("golden_in_zoom", None), ("golden_in_actions", Kind.TOOL_USE.value)):
                z = mcp.zoom(db, {"address": f"{q.session[:8]}:L{q.line}", "before": 0, "after": after, "brief": True,
                                  "kind": kind}, q.cut_ts, exclude)
                c[metric] += q.golden.casefold() in z.casefold()
        c["within_after"] += q.distance <= after
    return {"k": k, "after": after, "leaks": leaks, "by_generation": {g: dict(c) for g, c in sorted(per.items())}}


def mcp_config(path: Path, archive_db: Path, q: Question, exclude: tuple[str, ...]) -> Path:
    env = {"PYTHONPATH": str(REPO), "ETERNAL_ARCHIVE": str(archive_db), "ETERNAL_BEFORE": q.cut_ts,
           "ETERNAL_EXCLUDE": ",".join(exclude)}
    cfg = {"mcpServers": {"eternal": {"type": "stdio", "command": "python3", "args": ["-P", "-m", "eternal.mcp"],
                                      "env": env}}}
    path.write_text(json.dumps(cfg))
    return path


def transcript_copy(src: Path, cut_line: int, dst: Path) -> None:
    """The transcript's lines before the cut, with its own path, also in `~` form, pointing at the copy."""
    home, new = str(Path.home()), str(dst).encode()
    forms = [str(src).encode()] + ([("~" + str(src)[len(home):]).encode()] if str(src).startswith(home + "/") else [])
    with open(src, "rb") as f, open(dst, "wb") as out:
        for i, line in enumerate(f):
            if i >= cut_line:
                break
            for old in forms:
                line = line.replace(old, new)
            out.write(line)


def argv(system: System, model: str, cfg: Path | None, turns: int = TURNS, effort: Effort = EFFORT) -> list[str]:
    """`--restricted` confines file tools to the call's fresh cwd and skips user settings."""
    a = [str(WRAPPER), "-p", "--model", model, "--effort", effort.value, "--restricted", "--permission-prompts", "none",
         "--strict-mcp-config", "--disable-slash-commands", "--no-session-persistence",
         "--output-format", "stream-json", "--verbose"]
    if system == System.S4:
        return a + ["--tools", FILE_TOOLS, "--allowedTools", FILE_TOOLS, "--max-turns", str(turns)]
    if system in SEARCH:
        return a + ["--tools", "", "--mcp-config", str(cfg), "--allowedTools", TOOLS, "--max-turns", str(turns)]
    return a + ["--tools", "", "--max-turns", "2"]  # room to answer after a refused tool call


def parse(stdout: str) -> Call:
    """Answer, cost, usage and rate-limit utilization from a stream-json run."""
    events = [json.loads(x) for x in stdout.splitlines() if x.startswith("{")]
    out: Call = {"tool_calls": [], "limits": {}}
    for e in events:
        if e.get("type") == "assistant":
            out["model"] = e["message"].get("model", "")
            out["tool_calls"] += [b["name"] for b in e["message"].get("content", []) if b.get("type") == "tool_use"]
        elif e.get("type") == "rate_limit_event":
            for w, v in (e.get("rate_limit_info") or {}).get("unifiedWindows", {}).items():
                if isinstance(v, dict) and v.get("utilization") is not None:
                    out["limits"][w] = v["utilization"]
    res = [e for e in events if e.get("type") == "result"]
    if not res:
        out["error"] = "no result event"
        return out
    r = res[-1]
    out |= {"answer": r.get("result") or "", "cost_usd": r.get("total_cost_usd") or 0.0, "turns": r.get("num_turns"),
            "usage": {k: v for k, v in (r.get("usage") or {}).items() if k.endswith("tokens")},
            "denied": [str((d.get("tool_input") or {}).get("file_path") or (d.get("tool_input") or {}).get("path")
                           or d.get("tool_name")) for d in r.get("permission_denials") or []]}
    if r.get("is_error"):  # an API failure reports subtype "success" and the error as the result text
        sub = str(r.get("subtype"))
        out["error"] = sub if sub != "success" else "api: " + (r.get("result") or "")[:80]
    return out


def run(archive_db: Path, qs: list[Question], systems: list[System], out_dir: Path, model: str = "haiku",
        max_calls: int = 15, stop_at: float = 0.5, exclude: tuple[str, ...] = (),
        root: Path | None = None, turns: int = TURNS, effort: Effort = EFFORT) -> list[Call]:
    """Ask each question under each system via `claude -p`; resumable, capped by calls and rate-limit utilization.

    `root` is where the archive's transcripts live; S4 needs it for its cut copy. S4 skips a question whose
    summary does not point at its own transcript.
    """
    if System.S4 in systems and root is None:
        raise ValueError("S4 needs the transcripts root")
    out_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    log = out_dir / "calls.jsonl"
    calls: list[Call] = [json.loads(x) for x in log.read_text().splitlines()] if log.exists() else []
    done = {(c["key"], c["system"]) for c in calls}
    made, fails, limits, fmt = 0, 0, {}, registry.params()
    with closing(sqlite3.connect(f"file:{archive_db}?mode=ro", uri=True)) as db:
        for q, s in ((q, s) for q in qs for s in systems if (q.key, s.value) not in done):
            if made >= max_calls or any(v >= stop_at for v in limits.values()):
                break
            reg = registry_text(load(db, q.source_id), q)
            summary = summary_text(db, q)
            c: Call = {"key": q.key, "system": s.value, "generation": q.generation, "golden": q.golden,
                       "shown": listed(reg, q.line), "registry": fmt}
            if s == System.S4:
                src = Path(root) / db.execute("SELECT path FROM source WHERE id = ?", (q.source_id,)).fetchone()[0]
                foot = FOOTER.search(summary)
                if not foot or Path(foot.group(2)).name != src.name:
                    calls.append(c | {"skipped": "footer", "verdict": Verdict.UNCLEAR.value})
                    append(log, calls[-1])
                    continue
            box = Path(tempfile.mkdtemp(prefix="eternal-b5-", dir=SANDBOX))
            try:
                if s == System.S4:
                    copy = box / src.name
                    transcript_copy(src, q.cut_line, copy)
                    summary = FOOTER.sub(lambda m, path=str(copy): m.group(1) + path, summary)
                cfg = mcp_config(out_dir / f"{q.key}.mcp.json", archive_db, q, exclude) if s in SEARCH else None
                a = argv(s, model, cfg, turns, effort)
                t0 = time.monotonic()
                try:
                    p = subprocess.run(a, input=prompt(s, summary, reg, q), capture_output=True, text=True, cwd=box,
                                       timeout=TIMEOUT)
                    stdout, err = p.stdout, (p.stderr.strip()[-300:] if p.returncode else "")
                except subprocess.TimeoutExpired as e:
                    stdout = (e.stdout or b"").decode() if isinstance(e.stdout, bytes) else (e.stdout or "")
                    err = "timeout"
            finally:
                shutil.rmtree(box, ignore_errors=True)
            c |= parse(stdout)
            if err:
                c["error"] = c.get("error") or err
            c |= {"seconds": round(time.monotonic() - t0, 1), "argv": a[1:],
                  "verdict": verdict(c.get("answer", ""), q.golden).value}
            limits = c.get("limits") or limits
            made += 1
            if failed(c):  # kept out of the log, so a resumed run asks again
                (out_dir / f"{q.key}.{s.value}.failed.jsonl").write_text(stdout)
                append(out_dir / "failed.jsonl", c)
                fails += 1
                if fails >= MAX_FAILS:
                    break
                continue
            fails = 0
            (out_dir / f"{q.key}.{s.value}.jsonl").write_text(stdout)
            calls.append(c)
            append(log, c)
    return calls


def failed(c: Call) -> bool:
    """The call broke outside the model (API, network, auth, timeout); running out of turns is an answer."""
    return bool(c.get("error")) and c["error"] != "error_max_turns"


def append(log: Path, c: Call) -> None:
    fd = os.open(log, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    with os.fdopen(fd, "a") as f:
        f.write(json.dumps(c, ensure_ascii=False) + "\n")


def call_stratum(c: Call) -> Stratum:
    return Stratum.ALL if "shown" not in c else Stratum.SHOWN if c["shown"] else Stratum.HIDDEN


def label(c: Call) -> str:
    """The call's system, with its turn cap and effort where they differ from the defaults: S4@20/max."""
    a = c.get("argv") or []
    cap = a[a.index("--max-turns") + 1] if "--max-turns" in a else str(TURNS)
    effort = a[a.index("--effort") + 1] if "--effort" in a else EFFORT.value
    name = c["system"] if System(c["system"]) not in TOOLED or cap == str(TURNS) else f"{c['system']}@{cap}"
    return name if effort == EFFORT else f"{name}/{effort}"


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """95% Wilson interval for k successes of n."""
    if not n:
        return (0.0, 0.0)
    p, d = k / n, 1 + z * z / n
    mid, half = (p + z * z / (2 * n)) / d, z * (p * (1 - p) / n + z * z / (4 * n * n)) ** 0.5 / d
    return (max(0.0, mid - half), min(1.0, mid + half))


def load_calls(dirs: list[Path]) -> list[Call]:
    """Calls of several runs, e.g. a rerun of one system with another turn cap; a question asked twice under one
    label is an error."""
    calls = [json.loads(x) for d in dirs for x in (d / "calls.jsonl").read_text().splitlines()]
    seen = Counter((c["key"], label(c)) for c in calls)
    if dup := [k for k, n in seen.items() if n > 1]:
        raise ValueError(f"asked twice: {dup[:3]}")
    return calls


def report(calls: list[Call]) -> dict:
    """Per system label, stratum and generation: verdict counts over scored calls (`n`), turns and cost, and per
    stratum a total with a 95% interval. Skipped and refused calls are counted apart."""
    agg: dict[str, dict[str, dict[int, Counter]]] = defaultdict(lambda: defaultdict(lambda: defaultdict(Counter)))
    answer_turns: dict[tuple[str, str], list[int]] = defaultdict(list)  # turns of the calls that looked and were right
    cost: dict[tuple[str, str], float] = defaultdict(float)
    formats: list[dict | None] = []
    for c in calls:
        if c.get("registry") not in formats:
            formats.append(c.get("registry"))
        s, st = label(c), call_stratum(c).value
        a = agg[s][st][c["generation"]]
        if c.get("skipped"):
            a["skipped"] += 1
            continue
        right, looked = c["verdict"] == Verdict.CORRECT, bool(c.get("tool_calls"))
        if c.get("denied"):  # a refused read may have been a leak attempt: shown, not scored
            a["denied"] += 1
        else:
            a[c["verdict"]] += 1
            a["n"] += 1
            a["failed_no_tools"] += not right and not looked  # never looked
            a["tried"] += looked
            a["tried_correct"] += right and looked
            if right and looked:
                answer_turns[(s, st)].append(c.get("turns") or 0)
        a["turns"] += c.get("turns") or 0
        a["errors"] += bool(c.get("error"))
        a["out_of_turns"] += c.get("error") == "error_max_turns"
        cost[(s, st)] += c.get("cost_usd") or 0.0

    def total(s: str, st: str, gs: dict[int, Counter]) -> dict:
        t, ts = sum(gs.values(), Counter()), answer_turns[(s, st)]
        lo, hi = wilson(t["correct"], t["n"])
        asked = t["n"] + t["denied"]
        return dict(t) | {"ci95": [round(lo, 3), round(hi, 3)], "cost_usd": round(cost[(s, st)], 4),
                          "cost_per_call": round(cost[(s, st)] / asked, 4) if asked else None,
                          "answer_turns": {"mean": round(sum(ts) / len(ts), 1), "max": max(ts)} if ts else None}

    return {"calls": len(calls), "cost_usd": round(sum(cost.values()), 4), "registry_formats": formats,
            "pairs": pairs(calls),
            "systems": {s: {st: {"total": total(s, st, gs),
                                 "by_generation": {g: dict(v) for g, v in sorted(gs.items())}}
                            for st, gs in sorted(sts.items())}
                        for s, sts in sorted(agg.items())}}


def pairs(calls: list[Call]) -> dict:
    """Per stratum and pair of system labels scored on the same question: which one alone was right, both, or
    neither; `gave_up_*` splits the questions where at least one failed without a single tool call."""
    got: dict[tuple[str, str], dict[str, tuple[bool, bool]]] = defaultdict(dict)
    for c in calls:
        if not c.get("skipped") and not c.get("denied"):
            right = c["verdict"] == Verdict.CORRECT
            got[(call_stratum(c).value, c["key"])][label(c)] = (right, not right and not c.get("tool_calls"))
    out: dict[str, dict[str, Counter]] = defaultdict(lambda: defaultdict(Counter))

    def split(a: str, b: str, x: bool, y: bool) -> str:
        return "both" if x and y else a if x else b if y else "neither"

    for (st, _), res in got.items():
        for a, b in itertools.combinations(sorted(res), 2):
            (ra, ga), (rb, gb) = res[a], res[b]
            out[st][f"{a}-{b}"][split(a, b, ra, rb)] += 1
            if ga or gb:
                out[st][f"{a}-{b}"]["gave_up_" + split(a, b, ga, gb)] += 1
    return {st: {k: dict(v) for k, v in sorted(ps.items())} for st, ps in sorted(out.items())}
