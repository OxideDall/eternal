# Read-only: per-session compaction counts, transcript sizes, summary section structure. Counts only.
import json, os, glob, re
from collections import Counter
ROOT = os.path.expanduser("~/.claude/projects")
rows = []; hdr = Counter(); nsum = 0; lastver = Counter()
for p in glob.glob(f"{ROOT}/*/*.jsonl"):
    n = 0; size = os.path.getsize(p); v = None
    with open(p, "rb") as fh:
        for line in fh:
            if b'"compact_boundary"' in line and b'"subtype":"compact_boundary"' in line: n += 1
            if b'"isCompactSummary":true' in line:
                try: rec = json.loads(line)
                except ValueError: continue
                c = (rec.get("message") or {}).get("content")
                t = c if isinstance(c, str) else "\n".join(b.get("text","") for b in c or [] if isinstance(b, dict))
                nsum += 1
                for h in ("All user messages", "Pending Tasks", "Current Work", "Optional Next Step", "Files and Code Sections", "Errors and fixes"):
                    if h.lower() in t.lower(): hdr[h] += 1
            m = re.search(rb'"version":"(\d+\.\d+\.\d+)"', line)
            if m: v = m.group(1).decode()
    if n: rows.append((n, size)); lastver[v] += 1
rows.sort(reverse=True)
print("top-level files with boundaries:", len(rows), " boundaries:", sum(n for n,_ in rows))
print("compactions/session top10:", [n for n,_ in rows[:10]])
print("sizes MB of top10:", [round(s/1e6,1) for _,s in rows[:10]])
print("sessions with >=10 compactions:", sum(1 for n,_ in rows if n >= 10), " >=50:", sum(1 for n,_ in rows if n >= 50))
print("summaries:", nsum, " template sections present:", dict(hdr))
print("last version seen in compacted files:", dict(lastver.most_common(6)))
sub = glob.glob(f"{ROOT}/*/*/subagents/*.jsonl") + glob.glob(f"{ROOT}/*/*/*/subagents/*.jsonl")
sb = 0; sf = 0
for p in sub:
    with open(p, "rb") as fh:
        c = sum(1 for line in fh if b'"subtype":"compact_boundary"' in line)
    if c: sf += 1; sb += c
print("subagent files:", len(sub), " with boundaries:", sf, " boundaries:", sb)
