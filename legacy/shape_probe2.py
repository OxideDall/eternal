"""Counts-only probe: queued prompts, list-shaped user entries, chars/token, first-request size, Task ids."""
import hashlib
import json
import os
import re
import sys
from collections import Counter, defaultdict

ROOT = os.path.expanduser(sys.argv[1])
CYR = re.compile(r"[Ѐ-ӿ]")
LAT = re.compile(r"[A-Za-z]")
INTERRUPTS = {"[Request interrupted by user]", "[Request interrupted by user for tool use]"}


def pct(xs, q):
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(q * len(xs)))] if xs else None


def h(s):
    return hashlib.sha1(s.strip().encode()).hexdigest()


att_keys, qop_keys, qop_ops, snap_keys = Counter(), Counter(), Counter(), Counter()
qc_prompt_type, qc_mode, qc_origin = Counter(), Counter(), Counter()
list_shapes, interrupts = Counter(), Counter()
enq, deq_user, deq_att = set(), set(), set()
ratio = defaultdict(list)
first_req = []
task_res = Counter()
tur_keys = Counter()
for d, _, fs in os.walk(ROOT):
    if "/subagents" in d:
        continue
    for n in fs:
        if not n.endswith(".jsonl"):
            continue
        msgs = defaultdict(lambda: {"chars": 0, "kinds": set(), "out": 0, "text": ""})
        order = []
        seen_assistant = False
        humans_before = 0
        first = None
        task_ids = {}
        for raw in open(os.path.join(d, n), "rb"):
            try:
                e = json.loads(raw)
            except ValueError:
                continue
            t = e.get("type")
            if t == "attachment":
                a = e.get("attachment") or {}
                if a.get("type") == "queued_command":
                    att_keys.update(a.keys())
                    p = a.get("prompt")
                    qc_prompt_type[type(p).__name__] += 1
                    qc_mode[a.get("commandMode")] += 1
                    o = a.get("origin")
                    qc_origin[o.get("kind") if isinstance(o, dict) else o] += 1
                    if isinstance(p, str):
                        deq_att.add(h(p))
                elif a.get("type") == "prompt_snapshot":
                    snap_keys.update(a.keys())
            elif t == "queue-operation":
                qop_keys.update(e.keys())
                qop_ops[e.get("operation")] += 1
                if e.get("operation") == "enqueue" and isinstance(e.get("content"), str):
                    enq.add(h(e["content"]))
            elif t == "user":
                c = (e.get("message") or {}).get("content")
                if isinstance(c, str) and e.get("promptSource") == "queued":
                    deq_user.add(h(c))
                if isinstance(c, str) and c.strip() in INTERRUPTS:
                    interrupts["str"] += 1
                if isinstance(c, list):
                    kinds = tuple(sorted(Counter(b.get("type") for b in c if isinstance(b, dict)).items()))
                    if not e.get("origin") and not e.get("promptSource") and not e.get("isMeta") and not any(isinstance(b, dict) and b.get("type") == "tool_result" for b in c):
                        txt = "".join(b.get("text", "") for b in c if isinstance(b, dict) and b.get("type") == "text").strip()
                        list_shapes[(kinds, txt in INTERRUPTS)] += 1
                    for b in c:
                        if isinstance(b, dict) and b.get("type") == "tool_result" and b.get("tool_use_id") in task_ids:
                            cc = b.get("content")
                            s = cc if isinstance(cc, str) else "".join(x.get("text", "") for x in (cc or []) if isinstance(x, dict))
                            task_res["has_#N" if re.search(r"#\d+", s) else "no_#N"] += 1
                            tur = e.get("toolUseResult")
                            if isinstance(tur, dict):
                                tur_keys.update(tur.keys())
                                tk = tur.get("task")
                                if isinstance(tk, dict):
                                    tur_keys.update("task." + k for k in tk.keys())
                if not seen_assistant and isinstance(c, str) and (e.get("origin") or {}).get("kind") == "human":
                    humans_before += 1
                    if first is None:
                        first = {"chars": len(c), "ver": e.get("version"), "ts": e.get("timestamp")}
            elif t == "assistant":
                m = e.get("message") or {}
                mid = m.get("id")
                for b in m.get("content") or []:
                    if isinstance(b, dict):
                        msgs[mid]["kinds"].add(b.get("type"))
                        if b.get("type") == "text":
                            msgs[mid]["chars"] += len(b.get("text", ""))
                            msgs[mid]["text"] += b.get("text", "")
                        if b.get("type") == "tool_use" and b.get("name") == "TaskCreate":
                            task_ids[b.get("id")] = 1
                u = m.get("usage") or {}
                if mid:
                    msgs[mid]["out"] = u.get("output_tokens") or 0
                    if mid not in order:
                        order.append(mid)
                if not seen_assistant and mid and isinstance(u, dict):
                    seen_assistant = True
                    tot = (u.get("input_tokens") or 0) + (u.get("cache_creation_input_tokens") or 0) + (u.get("cache_read_input_tokens") or 0)
                    if first and humans_before == 1 and tot:
                        first_req.append((first["ver"] or "", first["ts"] or "", tot, first["chars"], u.get("cache_read_input_tokens") or 0))
        for mid in order:
            x = msgs[mid]
            if x["kinds"] == {"text"} and x["chars"] >= 300 and x["out"]:
                letters = len(CYR.findall(x["text"])) + len(LAT.findall(x["text"]))
                share = len(CYR.findall(x["text"])) / letters if letters else 0
                cls = "cyr>60%" if share > 0.6 else ("cyr<10%" if share < 0.1 else "mixed")
                ratio[cls].append(x["chars"] / x["out"])

print("queued_command attachment keys:", dict(att_keys))
print("  prompt type:", dict(qc_prompt_type), "commandMode:", dict(qc_mode), "origin:", dict(qc_origin))
print("prompt_snapshot keys:", dict(snap_keys))
print("queue-operation keys:", dict(qop_keys), "ops:", dict(qop_ops))
print(f"enqueue contents {len(enq)}; delivered as user(queued) {len(enq & deq_user)}; as queued_command attachment {len(enq & deq_att)}; neither {len(enq - deq_user - deq_att)}")
print("origin-less list user entries (block kinds, is_interrupt):", list_shapes.most_common(8))
print("interrupt str entries:", dict(interrupts))
print("chars per output token (text-only messages >=300 chars): ", {k: (len(v), round(pct(v, .5), 2), round(pct(v, .1), 2), round(pct(v, .9), 2)) for k, v in ratio.items()})
print("TaskCreate tool_result has #N:", dict(task_res), "toolUseResult keys:", dict(tur_keys.most_common(12)))
by = defaultdict(list)
for ver, ts, tot, ch, cr in first_req:
    m = re.match(r"(\d+)\.(\d+)\.(\d+)", ver)
    b = f"{m.group(1)}.{m.group(2)}.{int(m.group(3)) // 50 * 50}" if m else "?"
    by[b].append(tot)
print("first request total input tokens by version bucket (n, p10, p50, p90):")
for b in sorted(by):
    v = by[b]
    print("  ", b, len(v), pct(v, .1), pct(v, .5), pct(v, .9))
recent = [tot for ver, ts, tot, ch, cr in first_req if ts >= "2026-09-22"]
print("first request, last 30 days: n", len(recent), "p10", pct(recent, .1), "p50", pct(recent, .5), "p90", pct(recent, .9))
