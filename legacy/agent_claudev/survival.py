# Read-only: verbatim survival of human requests across successive compaction summaries.
# For a request typed in epoch k (before boundary k), check its 60-char probe in summaries k, k+1, ... Counts only.
import json, os, glob, re
from collections import defaultdict
ROOT = os.path.expanduser("~/.claude/projects")
def norm(t): return re.sub(r"\s+", " ", t).casefold()
def tb(c):
    if isinstance(c, str): return c
    if isinstance(c, list): return "\n".join(b.get("text","") for b in c if isinstance(b, dict) and b.get("type")=="text")
    return ""
def is_human(r):
    o = r.get("origin"); k = o.get("kind") if isinstance(o, dict) else None
    return k == "human" or (k is None and r.get("promptSource") in ("typed","sdk"))
alive = defaultdict(lambda: [0, 0])   # generation offset -> [present, total]
for p in glob.glob(f"{ROOT}/*/*.jsonl"):
    with open(p, "rb") as fh:
        if b'"compact_boundary"' not in fh.read(): continue
    epochs = [[]]; summaries = []; waiting = False
    with open(p, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            try: r = json.loads(line)
            except ValueError: continue
            if r.get("type") == "system" and r.get("subtype") == "compact_boundary":
                waiting = True; epochs.append([]); continue
            if waiting and r.get("type") == "user" and r.get("isCompactSummary"):
                summaries.append(norm(tb((r.get("message") or {}).get("content")))); waiting = False; continue
            if r.get("type") == "user" and not r.get("isMeta") and not r.get("isCompactSummary") and is_human(r):
                body = tb((r.get("message") or {}).get("content")).strip()
                if len(body) >= 20: epochs[-1].append(norm(body[:60]))
    for k, reqs in enumerate(epochs[:len(summaries)]):
        for probe in set(reqs):
            for j in range(k, len(summaries)):
                g = j - k
                alive[g][1] += 1
                alive[g][0] += probe in summaries[j]
for g in (0, 1, 2, 3, 5, 10, 20, 50):
    if g in alive:
        a, t = alive[g]; print(f"generation +{g:>2}: verbatim present {a}/{t} = {a/t:.0%}")
