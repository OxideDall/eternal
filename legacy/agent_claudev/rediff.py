# Read-only: re-diff claudevs done-snapshots against saved summaries, per fact kind. Prints counts only.
import json, re, sys, glob, os
from collections import Counter
STATE = os.path.expanduser("~/.local/state/claudevs")
def norm(t): return re.sub(r"\s+", " ", t).casefold()
def text_blocks(c):
    if isinstance(c, str): return c
    if isinstance(c, list): return "\n".join(b.get("text","") for b in c if isinstance(b, dict) and b.get("type")=="text")
    return ""
def summaries_from_transcript(sid):
    out = []
    for p in glob.glob(os.path.expanduser(f"~/.claude/projects/*/{sid}.jsonl")):
        with open(p, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if '"isCompactSummary":true' not in line.replace(" ", ""): continue
                try: rec = json.loads(line)
                except ValueError: continue
                body = text_blocks((rec.get("message") or {}).get("content"))
                if body: out.append((rec.get("timestamp"), body))
    return out
for snap_path in sorted(glob.glob(f"{STATE}/snapshots/*.json")):
    snap = json.load(open(snap_path, encoding="utf-8"))
    sid = snap["session_id"]; base = os.path.basename(snap_path)
    m = re.match(r".*\.(\d+)\.done\.json$", base)
    done_at = int(m.group(1)) if m else None
    # pick saved summary whose ns is closest to done_at
    cands = []
    for f in glob.glob(f"{STATE}/summaries/{sid}-*.md"):
        ns = int(f.rsplit("-",1)[1][:-3]); cands.append((abs(ns/1e9 - (done_at or 0)), f))
    src, summary = "none", ""
    if cands and done_at:
        d, f = min(cands)
        if d < 30: src, summary = f"probe-file(dt={d:.1f}s)", open(f, encoding="utf-8").read()
    if not summary and done_at:
        ts = summaries_from_transcript(sid)
        if ts: src, summary = f"transcript(n={len(ts)}, picked last)", ts[-1][1]
    kinds = Counter(f["kind"] for f in snap["facts"])
    hay = norm(summary)
    miss = Counter(f["kind"] for f in snap["facts"] if norm(f["probe"]) not in hay) if summary else None
    print(json.dumps({"snap": sid[:8] + ("…done@" + str(done_at) if done_at else "…pending"),
                      "facts_by_kind": dict(kinds), "summary_src": src, "summary_chars": len(summary),
                      "missing_by_kind": dict(miss) if miss is not None else None}, ensure_ascii=False))
