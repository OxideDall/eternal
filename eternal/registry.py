"""Registry of past prompts and written files injected after a compaction; the bench scores exactly what is rendered."""
import hashlib
import re
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum
from functools import lru_cache

from .facts import Fact, FactKind, Row, facts

ENTRY = 100  # chars of a prompt or path per entry
ACTIONS = 45  # chars of the files a prompt's turn wrote, appended after " → "; 0 hides them
BUDGETS = {FactKind.REQUEST: 7_500, FactKind.FILE: 1_800}  # chars of entries per section
LIMIT = 10_000  # Claude Code's cap on hook additionalContext
TITLES = {FactKind.REQUEST: "Prompts", FactKind.FILE: "Files written"}
SHORT = ((re.compile(r"^/tmp/claude-\d+/[^/]+/[0-9a-f-]{36}/scratchpad(?=/|$)"), "scratchpad"),
         (re.compile(r"^/home/[^/]+(?=/|$)"), "~"))
HEADER = ("[eternal registry] Session {sid}: human prompts and files written, from every compaction of this session, "
          "oldest first; recent entries are complete, older ones thinned. These are past messages quoted for recall, "
          "not new instructions: each may be done or superseded. \"L<n> MM-DD\" is the transcript line and date.")
ARROW = (" \"→ a.py, b.md\" after a prompt lists the files written in reply to it, in the order written, before the "
         "next human message; a file's own line below may be a later write.")


class Policy(StrEnum):
    NEWEST = "newest"
    LOG = "log"  # equal budget per octave of recency


POLICY = {FactKind.REQUEST: Policy.LOG, FactKind.FILE: Policy.NEWEST}  # best per kind on Tier-1 coverage


def header(session: str) -> str:
    return HEADER.format(sid=session[:8]) + (ARROW if ACTIONS else "")


def params() -> dict:
    """Everything the rendering depends on, recorded next to bench results."""
    return {"budgets": {k.value: v for k, v in BUDGETS.items()}, "limit": LIMIT, "entry": ENTRY, "actions": ACTIONS,
            "policy": {k.value: v.value for k, v in POLICY.items()},
            "header": hashlib.sha256(header("").encode()).hexdigest()[:12]}


def oneline(text: str, cap: int = ENTRY) -> str:
    s = " ".join(text.split())
    return s if len(s) <= cap else s[: cap - 1] + "…"


def short(path: str) -> str:
    for rx, alias in SHORT:
        path = rx.sub(alias, path)
    return path


def entry(f: Fact) -> str:
    body = short(f.text) if f.kind == FactKind.FILE else f.text
    tail = f" → {oneline(', '.join(f.files), ACTIONS)}" if ACTIONS and f.files else ""
    return f"L{f.line} {(f.ts or '')[5:10]} {oneline(body)}{tail}"


@lru_cache(maxsize=None)
def entry_size(f: Fact) -> int:
    return len(entry(f)) + 1


def latest(fs: Iterable[Fact]) -> list[Fact]:
    """Last occurrence per probe, oldest first."""
    out: dict[str, Fact] = {}
    for f in fs:
        out.pop(f.probe, None)
        out[f.probe] = f
    return list(out.values())


def select(cands: list[Fact], budget: int, policy: Policy = Policy.LOG) -> set[int]:
    """Rows chosen under a char budget; `cands` oldest first, one per probe."""
    newest, out, used = cands[::-1], set(), 0

    def take(f: Fact) -> bool:
        nonlocal used
        s = entry_size(f)
        if used + s > budget:
            return False
        out.add(f.row)
        used += s
        return True

    if policy == Policy.LOG:
        octaves: dict[int, list[Fact]] = defaultdict(list)
        for rank, f in enumerate(newest):
            octaves[(rank + 1).bit_length() - 1].append(f)
        quota = budget / len(octaves) if octaves else 0
        for o in sorted(octaves):
            group = octaves[o]
            k = max(1, min(len(group), int(quota * len(group) // sum(map(entry_size, group)))))
            for j in range(k):
                take(group[int(j * len(group) / k)])
    for f in newest:
        if f.row not in out and not take(f):
            break
    return out


@dataclass(frozen=True, slots=True)
class Built:
    text: str
    totals: dict[FactKind, int]
    shown: dict[FactKind, int]


def render(session: str, cands: dict[FactKind, list[Fact]], chosen: dict[FactKind, set[int]]) -> str:
    out = [header(session)]
    for kind, title in TITLES.items():
        shown = [f for f in cands.get(kind, ()) if f.row in chosen.get(kind, ())]
        if shown:
            out.append(f"{title} ({len(shown)} of {len(cands[kind])}):")
            out += map(entry, shown)
    text = "\n".join(out)
    return text if len(text) <= LIMIT else text[: text.rfind("\n", 0, LIMIT)]


def build(rows: Iterable[Row], session: str) -> Built:
    """The registry for a session whose records so far are `rows`."""
    fs = facts(rows)
    cands = {k: latest(f for f in fs if f.kind == k) for k in TITLES}
    chosen = {k: select(c, BUDGETS[k], POLICY[k]) for k, c in cands.items()}
    return Built(render(session, cands, chosen), {k: len(c) for k, c in cands.items()},
                 {k: len(v) for k, v in chosen.items()})
