# Read-only corpus stats over top-level transcripts: compaction boundaries, summaries,
# preserved segments, todo-tool usage, and an offline replay of state_guard's
# USER_REQUEST / TOUCHED_FILE / TODO probes. Prints aggregate counts only.
import json, re, os, glob, statistics
from collections import Counter, defaultdict, deque
ROOT = os.path.expanduser("~/.claude/projects")
WRITE_TOOLS = {"Edit","Write","NotebookEdit","MultiEdit","FileEdit","FileWrite"}
TODO_TOOLS = ("TodoWrite","TaskCreate","TaskUpdate","TaskList","TaskGet","TaskStop","TaskOutput")
def norm(t): return re.sub(r"\s+", " ", t).casefold()
def text_blocks(c):
    if isinstance(c, str): return c
    if isinstance(c, list): return "\n".join(b.get("text","") for b in c if isinstance(b, dict) and b.get("type")=="text")
    return ""
def is_human(rec):
    o = rec.get("origin"); k = o.get("kind") if isinstance(o, dict) else None
    return k == "human" or (k is None and rec.get("promptSource") in ("typed","sdk"))
def vkey(v):
    m = re.match(r"(\d+)\.(\d+)\.(\d+)", v or "")
    return tuple(map(int, m.groups())) if m else (0,0,0)
def vbucket(v):
    t = vkey(v)
    if t == (0,0,0): return "?"
    b = t[2] // 50 * 50
    return f"{t[0]}.{t[1]}.{b:03d}-{b+49:03d}"
files = [p for p in glob.glob(f"{ROOT}/*/*.jsonl")]
bounds = []          # one dict per boundary
tool_by_ver = defaultdict(Counter)
replay = []
for path in files:
    with open(path, "rb") as fh:
        raw = fh.read()
    if b'"compact_boundary"' not in raw:
        # still count todo tools for version usage
        pass
    lines = raw.split(b"\n")
    uuid_line = {}
    epoch = []      # (lineno, rec) since last boundary
    pending = None  # boundary waiting for its summary
    for i, line in enumerate(lines):
        if not line.startswith(b"{"): continue
        try: rec = json.loads(line)
        except ValueError: continue
        u = rec.get("uuid")
        if u and u not in uuid_line: uuid_line[u] = i
        t = rec.get("type"); ver = rec.get("version")
        if t == "assistant":
            for b in (rec.get("message") or {}).get("content") or []:
                if isinstance(b, dict) and b.get("type") == "tool_use" and b.get("name") in TODO_TOOLS:
                    tool_by_ver[vbucket(ver)][b["name"]] += 1
        if t == "system" and rec.get("subtype") == "compact_boundary":
            md = rec.get("compactMetadata") or {}
            pm = (md.get("preservedMessages") or {}).get("uuids") or []
            before = sum(1 for x in pm if x in uuid_line and uuid_line[x] < i)
            in_epoch_lines = {ln for ln, _ in epoch}
            in_epoch = sum(1 for x in pm if x in uuid_line and uuid_line[x] in in_epoch_lines)
            # --- replay state_guard facts on the epoch that preceded this boundary
            reqs = deque(maxlen=12); written = {}; todos = []
            for ln, r in epoch:
                k = r.get("type"); msg = r.get("message") or {}
                if k == "user" and not r.get("isMeta") and not r.get("isCompactSummary"):
                    body = text_blocks(msg.get("content")).strip()
                    if body and is_human(r): reqs.append((body[:400], r.get("uuid")))
                elif k == "assistant":
                    for b in msg.get("content") or []:
                        if not isinstance(b, dict) or b.get("type") != "tool_use": continue
                        n, a = b.get("name"), b.get("input") or {}
                        if n in WRITE_TOOLS and a.get("file_path"): written.setdefault(str(a["file_path"]), r.get("uuid"))
                        elif n == "TodoWrite" and isinstance(a.get("todos"), list): todos = a["todos"]
            facts = []
            seen = set()
            for body, uid in reqs:
                if body in seen: continue
                seen.add(body); facts.append(("user_request", body[:60], uid))
            for td in todos:
                if isinstance(td, dict) and td.get("status") != "completed":
                    lab = str(td.get("content") or td.get("activeForm") or "").strip()
                    if lab: facts.append(("todo", lab[:40], None))
            for p, uid in list(written.items())[:40]:
                facts.append(("touched_file", os.path.basename(p), uid))
            pending = {"ver": ver, "trigger": md.get("trigger"), "pre": md.get("preTokens"), "post": md.get("postTokens"),
                       "dur": md.get("durationMs"), "pre_c": md.get("precomputed"), "n_pres": len(pm),
                       "pres_before": before, "pres_in_epoch": in_epoch, "facts": facts, "pm": set(pm),
                       "summary": None}
            bounds.append(pending)
            epoch = []
            continue
        if pending is not None and t == "user" and rec.get("isCompactSummary"):
            pending["summary"] = text_blocks((rec.get("message") or {}).get("content"))
            pending = None
            continue
        epoch.append((i, rec))
def pct(xs, q):
    xs = sorted(xs); 
    return xs[min(len(xs)-1, int(q*(len(xs)-1)+0.5))] if xs else None
print("top-level files:", len(files), " with boundaries:", sum(1 for _ in set()) )
print("boundaries:", len(bounds), " with summary:", sum(1 for b in bounds if b["summary"]))
print("trigger:", dict(Counter(b["trigger"] for b in bounds)))
print("precomputed:", dict(Counter(b["pre_c"] for b in bounds)))
byver = defaultdict(list)
for b in bounds: byver[vbucket(b["ver"])].append(b)
for vb in sorted(byver):
    bs = byver[vb]
    sl = [len(b["summary"]) for b in bs if b["summary"]]
    pres = [b for b in bs if b["n_pres"]]
    print(f"[{vb}] n={len(bs)} auto={sum(b['trigger']=='auto' for b in bs)} precomputed={sum(bool(b['pre_c']) for b in bs)}"
          f" summary_chars p50={pct(sl,.5)} p90={pct(sl,.9)} max={max(sl) if sl else None}"
          f" with_preserved={len(pres)} preserved_msgs p50={pct([b['n_pres'] for b in pres],.5)}"
          f" pres_before_boundary={sum(b['pres_before'] for b in pres)}/{sum(b['n_pres'] for b in pres)}"
          f" pres_in_epoch={sum(b['pres_in_epoch'] for b in pres)}")
pre = [b["pre"] for b in bounds if isinstance(b["pre"], int)]; post = [b["post"] for b in bounds if isinstance(b["post"], int)]
dur = [b["dur"] for b in bounds if isinstance(b["dur"], (int,float))]
print("preTokens p50/p90/max:", pct(pre,.5), pct(pre,.9), max(pre) if pre else None, " n=", len(pre))
print("postTokens p50/p90/max:", pct(post,.5), pct(post,.9), max(post) if post else None, " n=", len(post))
print("durationMs p50/p90/max:", pct(dur,.5), pct(dur,.9), max(dur) if dur else None, " n=", len(dur))
print("todo-tool calls by version bucket:")
for vb in sorted(tool_by_ver): print("  ", vb, dict(tool_by_ver[vb]))
# replay
kinds = Counter(); miss = Counter(); miss_adj = Counter(); per_b = []; per_b_adj = []; nf = []
for b in bounds:
    if not b["summary"]: continue
    hay = norm(b["summary"]); m = 0; ma = 0
    for kind, probe, uid in b["facts"]:
        kinds[kind] += 1
        if norm(probe) not in hay:
            miss[kind] += 1; m += 1
            if uid not in b["pm"]: miss_adj[kind] += 1; ma += 1
    nf.append(len(b["facts"])); per_b.append(m); per_b_adj.append(ma)
print("replay facts by kind:", dict(kinds))
print("replay missing by kind (verbatim probe):", dict(miss))
print("replay missing excl. preserved-message sources:", dict(miss_adj))
print("facts/boundary p50/p90/max:", pct(nf,.5), pct(nf,.9), max(nf) if nf else None)
print("missing/boundary p50/p90/max:", pct(per_b,.5), pct(per_b,.9), max(per_b) if per_b else None,
      " zero-missing boundaries:", sum(1 for x in per_b if x == 0), "/", len(per_b))
print("missing/boundary excl.preserved p50/p90/max:", pct(per_b_adj,.5), pct(per_b_adj,.9), max(per_b_adj) if per_b_adj else None)
