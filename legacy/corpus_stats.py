"""Counts-only stats over Claude Code transcripts: sessions, messages, compactions; never prints content."""
import json
import os
import sys
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime

ROOT = os.path.expanduser(sys.argv[1])


@dataclass
class Session:
    path: str
    project: str
    sub: bool
    bytes: int = 0
    prompts: int = 0
    tool_results: int = 0
    texts: int = 0
    tool_uses: int = 0
    compactions: int = 0
    pre_tokens: list = field(default_factory=list)
    summary_chars: list = field(default_factory=list)
    first: str = ""
    last: str = ""

    @property
    def messages(self) -> int:
        return self.prompts + self.tool_results + self.texts + self.tool_uses


def scan(path: str, project: str, sub: bool) -> Session:
    s = Session(path, project, sub, os.path.getsize(path))
    with open(path, "rb") as f:
        for raw in f:
            try:
                e = json.loads(raw)
            except ValueError:
                continue
            ts = e.get("timestamp") or ""
            if ts:
                s.first = min(s.first, ts) if s.first else ts
                s.last = max(s.last, ts)
            kind = e.get("type")
            if kind == "system" and e.get("subtype") == "compact_boundary":
                s.compactions += 1
                pre = (e.get("compactMetadata") or {}).get("preTokens")
                if isinstance(pre, int):
                    s.pre_tokens.append(pre)
                continue
            msg = e.get("message") or {}
            content = msg.get("content")
            if kind == "user":
                if e.get("isCompactSummary"):
                    text = content if isinstance(content, str) else "".join(b.get("text", "") for b in content if isinstance(b, dict))
                    s.summary_chars.append(len(text))
                    continue
                if isinstance(content, str):
                    s.prompts += 1
                elif isinstance(content, list):
                    if any(isinstance(b, dict) and b.get("type") == "tool_result" for b in content):
                        s.tool_results += sum(1 for b in content if isinstance(b, dict) and b.get("type") == "tool_result")
                    else:
                        s.prompts += 1
            elif kind == "assistant" and isinstance(content, list):
                for b in content:
                    if not isinstance(b, dict):
                        continue
                    if b.get("type") == "text":
                        s.texts += 1
                    elif b.get("type") == "tool_use":
                        s.tool_uses += 1
    return s


def days(s: Session) -> float:
    if not s.first or not s.last:
        return 0.0
    a = datetime.fromisoformat(s.first.replace("Z", "+00:00"))
    b = datetime.fromisoformat(s.last.replace("Z", "+00:00"))
    return (b - a).total_seconds() / 86400


def pct(xs: list, q: float) -> float:
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(q * len(xs)))] if xs else 0


sessions = []
for dirpath, _, files in os.walk(ROOT):
    for name in files:
        if name.endswith(".jsonl"):
            path = os.path.join(dirpath, name)
            rel = os.path.relpath(path, ROOT)
            sessions.append(scan(path, rel.split(os.sep)[0], "/subagents/" in path))

main = [s for s in sessions if not s.sub]
subs = [s for s in sessions if s.sub]
for label, group in (("main", main), ("subagent", subs)):
    msgs = [s.messages for s in group]
    print(f"{label}: {len(group)} files, {sum(s.bytes for s in group) / 1e9:.2f} GB, messages {sum(msgs)} "
          f"(p50 {pct(msgs, .5)}, p90 {pct(msgs, .9)}, max {max(msgs, default=0)}), prompts {sum(s.prompts for s in group)}, "
          f"tool calls {sum(s.tool_uses for s in group)}, compactions {sum(s.compactions for s in group)}")
firsts = sorted(s.first for s in main if s.first)
print(f"main span: {firsts[0][:10] if firsts else '-'} .. {max((s.last for s in main), default='')[:10]}")
comp = [s for s in main if s.compactions]
print(f"main sessions with compactions: {len(comp)}; compactions per such session p50 {pct([s.compactions for s in comp], .5)}, max {max((s.compactions for s in comp), default=0)}")
pre = [p for s in main for p in s.pre_tokens]
summ = [c for s in main for c in s.summary_chars]
print(f"preTokens at compaction: n={len(pre)} p50 {pct(pre, .5)} p90 {pct(pre, .9)} max {max(pre, default=0)}")
print(f"compact summary chars: n={len(summ)} p50 {pct(summ, .5)} p90 {pct(summ, .9)} max {max(summ, default=0)}")
print("projects (main sessions, messages):")
by = Counter()
cnt = Counter()
for s in main:
    by[s.project] += s.messages
    cnt[s.project] += 1
for p, m in by.most_common(12):
    print(f"  {p[:60]:60} {cnt[p]:4} {m:8}")
print("top main sessions by messages (sid8, project, messages, prompts, compactions, days):")
for s in sorted(main, key=lambda s: -s.messages)[:12]:
    sid = os.path.basename(s.path)[:8]
    print(f"  {sid} {s.project[:40]:40} {s.messages:7} {s.prompts:5} {s.compactions:4} {days(s):6.1f}")
