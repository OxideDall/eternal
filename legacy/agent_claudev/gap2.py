# Read-only: same as gap.py but stop at ANY prompt-like user entry (human, sdk, system/task-notification).
import json, os, glob
from collections import Counter
ROOT = os.path.expanduser("~/.claude/projects")
def prompt_like(r):
    if r.get("type") != "user" or r.get("isMeta") or r.get("isCompactSummary"): return None
    c = (r.get("message") or {}).get("content")
    if isinstance(c, list) and any(isinstance(b, dict) and b.get("type") == "tool_result" for b in c): return None
    o = r.get("origin"); k = o.get("kind") if isinstance(o, dict) else None
    return f"{k or '-'}|{r.get('promptSource') or '-'}"
res = []; never = 0; kinds = Counter()
for p in glob.glob(f"{ROOT}/*/*.jsonl"):
    with open(p, "rb") as fh:
        if b'"compact_boundary"' not in fh.read(): continue
    cur = None
    with open(p, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            try: r = json.loads(line)
            except ValueError: continue
            if r.get("type") == "system" and r.get("subtype") == "compact_boundary":
                if cur is not None: never += 1
                cur = 0 if (r.get("compactMetadata") or {}).get("trigger") == "auto" else None
                continue
            if cur is None: continue
            if r.get("type") == "assistant":
                cur += sum(1 for b in (r.get("message") or {}).get("content") or [] if isinstance(b, dict) and b.get("type") == "tool_use")
                continue
            k = prompt_like(r)
            if k: kinds[k] += 1; res.append(cur); cur = None
    if cur is not None: never += 1
def pct(xs, q):
    xs = sorted(xs); return xs[min(len(xs)-1, int(q*(len(xs)-1)+0.5))] if xs else None
print(f"auto: reached any prompt n={len(res)}, next-boundary-or-EOF first={never}; tool_calls p50={pct(res,.5)} p90={pct(res,.9)} max={max(res) if res else None}")
print("first prompt kinds (origin.kind|promptSource):", dict(kinds.most_common(8)))
