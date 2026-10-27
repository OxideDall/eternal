# Read-only: for compactions whose replayed diff has missing>0, what comes first:
# a human prompt (delivered), another compaction with missing>0 (pending overwritten), or EOF.
import json, os, glob, re
from collections import Counter, deque
ROOT = os.path.expanduser("~/.claude/projects")
WRITE = {"Edit","Write","NotebookEdit","MultiEdit","FileEdit","FileWrite"}
def norm(t): return re.sub(r"\s+", " ", t).casefold()
def tb(c):
    if isinstance(c, str): return c
    if isinstance(c, list): return "\n".join(b.get("text","") for b in c if isinstance(b, dict) and b.get("type")=="text")
    return ""
def human(r):
    o = r.get("origin"); k = o.get("kind") if isinstance(o, dict) else None
    return k == "human" or (k is None and r.get("promptSource") in ("typed","sdk"))
out = Counter()
for p in glob.glob(f"{ROOT}/*/*.jsonl"):
    with open(p, "rb") as fh:
        if b'"compact_boundary"' not in fh.read(): continue
    events = []            # ("B", facts) / ("H",)
    epoch = []; waiting = None
    with open(p, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            try: r = json.loads(line)
            except ValueError: continue
            t = r.get("type")
            if t == "system" and r.get("subtype") == "compact_boundary":
                reqs = deque(maxlen=12); written = {}; todos = []
                for e in epoch:
                    m = e.get("message") or {}
                    if e.get("type") == "user" and not e.get("isMeta") and not e.get("isCompactSummary"):
                        b = tb(m.get("content")).strip()
                        if b and human(e): reqs.append(b[:400])
                    elif e.get("type") == "assistant":
                        for blk in m.get("content") or []:
                            if isinstance(blk, dict) and blk.get("type") == "tool_use":
                                a = blk.get("input") or {}
                                if blk.get("name") in WRITE and a.get("file_path"): written.setdefault(str(a["file_path"]), 1)
                                elif blk.get("name") == "TodoWrite" and isinstance(a.get("todos"), list): todos = a["todos"]
                probes = [x[:60] for x in dict.fromkeys(reqs)]
                probes += [str(td.get("content") or "")[:40] for td in todos if isinstance(td, dict) and td.get("status") != "completed" and td.get("content")]
                probes += [os.path.basename(f) for f in list(written)[:40]]
                waiting = {"trig": (r.get("compactMetadata") or {}).get("trigger"), "probes": probes}
                events.append(waiting); epoch = []; continue
            if waiting is not None and t == "user" and r.get("isCompactSummary"):
                hay = norm(tb((r.get("message") or {}).get("content")))
                waiting["missing"] = sum(1 for pr in waiting["probes"] if norm(pr) not in hay)
                waiting = None; continue
            if t == "user" and not r.get("isMeta") and not r.get("isCompactSummary") and human(r):
                events.append("H")
            epoch.append(r)
    for i, ev in enumerate(events):
        if ev == "H" or not ev.get("missing"): continue
        fate = "eof"
        for nxt in events[i+1:]:
            if nxt == "H": fate = "delivered"; break
            if nxt.get("missing"): fate = "overwritten"; break
        out[(ev["trig"], fate)] += 1
for k in sorted(out): print(k, out[k])
print("total with missing>0:", sum(out.values()))
