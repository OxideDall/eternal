# Read-only: tool calls / wall time between each compaction boundary and the next human prompt.
import json, os, glob
from datetime import datetime
ROOT = os.path.expanduser("~/.claude/projects")
def is_human(rec):
    o = rec.get("origin"); k = o.get("kind") if isinstance(o, dict) else None
    return k == "human" or (k is None and rec.get("promptSource") in ("typed","sdk"))
def ts(r):
    try: return datetime.fromisoformat(r["timestamp"].replace("Z","+00:00")).timestamp()
    except Exception: return None
res = {"auto": [], "manual": []}; tsec = {"auto": [], "manual": []}; never = {"auto":0,"manual":0}
for p in glob.glob(f"{ROOT}/*/*.jsonl"):
    with open(p, "rb") as fh:
        if b'"compact_boundary"' not in fh.read(): continue
    cur = None
    with open(p, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            try: r = json.loads(line)
            except ValueError: continue
            t = r.get("type")
            if t == "system" and r.get("subtype") == "compact_boundary":
                if cur: never[cur["trig"]] += 1
                cur = {"trig": (r.get("compactMetadata") or {}).get("trigger") or "auto", "tools": 0, "t0": ts(r)}
                continue
            if cur is None: continue
            if t == "assistant":
                cur["tools"] += sum(1 for b in (r.get("message") or {}).get("content") or [] if isinstance(b, dict) and b.get("type") == "tool_use")
            elif t == "user" and not r.get("isMeta") and not r.get("isCompactSummary") and is_human(r):
                res[cur["trig"]].append(cur["tools"])
                t1 = ts(r)
                if t1 and cur["t0"]: tsec[cur["trig"]].append(t1 - cur["t0"])
                cur = None
    if cur: never[cur["trig"]] += 1
def pct(xs, q):
    xs = sorted(xs); return xs[min(len(xs)-1, int(q*(len(xs)-1)+0.5))] if xs else None
for k in res:
    xs = res[k]; ts_ = tsec[k]
    print(f"{k}: n={len(xs)} (+{never[k]} without a next prompt) tool_calls_before_next_prompt p50={pct(xs,.5)} p90={pct(xs,.9)} max={max(xs) if xs else None}"
          f" zero={sum(1 for x in xs if x==0)}  minutes p50={round(pct(ts_,.5)/60,1) if ts_ else None} p90={round(pct(ts_,.9)/60,1) if ts_ else None}")
