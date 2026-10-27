"""CLI: freeze a corpus, ingest it, run Tier-1 benches, check against a baseline, search the archive."""
import argparse
import getpass
import json
import os
import sys
import time
from collections import Counter
from enum import StrEnum
from pathlib import Path

from . import archive, corpus, doctor, hook, live, registry, selftest
from .bench import prefix, qa, replay, resolution
from .bench.report import markdown
from .bench.timeline import timelines


def data_dir(arg: str | None) -> Path:
    return Path(arg or os.environ.get("ETERNAL_DATA") or Path.home() / ".local/share/eternal").expanduser()


def open_db(a: argparse.Namespace) -> archive.sqlite3.Connection:
    path = data_dir(a.data) / "archive.db"
    if not path.exists():
        raise SystemExit(f"{path}: no archive; run ingest first")
    return archive.connect(path)


def corpus_counts(db) -> dict:
    one = lambda sql: db.execute(sql).fetchone()[0]
    meta = dict(db.execute("SELECT key, value FROM meta"))
    return {
        "files": one("SELECT count(*) FROM source WHERE sub = 0"),
        "records": one("SELECT count(*) FROM record"),
        "kinds": dict(db.execute("SELECT kind, count(*) FROM record GROUP BY kind")),
        "boundaries": one("SELECT count(*) FROM boundary"),
        "requests": one("SELECT count(*) FROM request"),
        "redacted": json.loads(meta.get("redacted", "{}")),
    }


def tier1(db, m: corpus.Manifest) -> dict:
    stored = dict(db.execute("SELECT key, value FROM meta")).get("manifest")
    if stored != m.digest():
        raise SystemExit("archive was ingested from a different manifest")
    return json.loads(json.dumps({
        "manifest": m.digest(), "created": m.created, "corpus": corpus_counts(db),
        "b1": replay.run(db, list(timelines(db, with_cuts=True)), m.created),
        "b2": resolution.run(timelines(db)),
        "b3": prefix.run(db, m.created),
    }))


def cmd_freeze(a: argparse.Namespace) -> None:
    m = corpus.freeze(Path(a.root), a.sub, frozenset(a.exclude))
    Path(a.out).write_text(m.dumps())
    print(f"{len(m.files)} files, {sum(f.size for f in m.files) / 1e9:.2f} GB, manifest {m.digest()[:12]}")


def cmd_ingest(a: argparse.Namespace) -> None:
    m = corpus.Manifest.load(Path(a.manifest))
    bad = corpus.verify(m)
    if bad:
        raise SystemExit(f"{len(bad)} frozen prefixes changed, e.g. {bad[0]}")
    path = data_dir(a.data) / "archive.db"
    archive.check_space(path)
    db = archive.connect(path)
    t0 = time.monotonic()
    st = archive.ingest(db, m, (lambda p: print(p, file=sys.stderr)) if a.verbose else None)
    meta = dict(db.execute("SELECT key, value FROM meta"))
    red = json.loads(meta.get("redacted", "{}"))
    for k, v in st.redacted.items():
        red[k] = red.get(k, 0) + v
    db.executemany("INSERT OR REPLACE INTO meta VALUES (?, ?)",
                   [("manifest", m.digest()), ("redacted", json.dumps(red, sort_keys=True))])
    db.commit()
    print(json.dumps({"files": st.files, "lines": st.lines, "bad_json": st.bad_json, "boundaries": st.boundaries,
                      "unsummarized": st.unsummarized, "requests": st.requests, "kinds": dict(st.kinds),
                      "redacted": dict(st.redacted), "seconds": round(time.monotonic() - t0, 1),
                      "db_bytes": path.stat().st_size}, ensure_ascii=False))


def cmd_bench(a: argparse.Namespace) -> None:
    db = open_db(a)
    r = tier1(db, corpus.Manifest.load(Path(a.manifest)))
    Path(a.out).write_text(json.dumps(r, ensure_ascii=False, indent=1) + "\n")
    if a.md:
        Path(a.md).write_text(markdown(r))
    print(f"wrote {a.out}" + (f" and {a.md}" if a.md else ""))


def _diff(a, b, path: str = "") -> list[str]:
    if isinstance(a, dict) and isinstance(b, dict):
        return [d for k in sorted(set(a) | set(b)) for d in _diff(a.get(k), b.get(k), f"{path}/{k}")]
    return [] if a == b else [f"{path}: baseline {b!r} now {a!r}"]


def cmd_check(a: argparse.Namespace) -> None:
    db = open_db(a)
    now = tier1(db, corpus.Manifest.load(Path(a.manifest)))
    diffs = _diff(now, json.loads(Path(a.baseline).read_text()))
    for d in diffs[:50]:
        print(d)
    print(f"{len(diffs)} differences")
    sys.exit(1 if diffs else 0)


def cmd_search(a: argparse.Namespace) -> None:
    db = open_db(a)
    index = archive.Index.TRIGRAM if a.trigram else archive.Index.WORDS
    query = a.query if a.raw else archive.quote(a.query)
    for h in archive.search(db, query, index, a.k):
        print(f"#{h.id} {h.session[:8]} {(h.ts or '')[:16]} {h.author}/{h.kind.value}: {h.snippet}")


def cmd_audit(a: argparse.Namespace) -> None:
    root = Path(a.root).expanduser() if a.root else live.project_dir(selftest.REPO)
    total, per, ahead = Counter(), {}, 0
    for p in sorted(root.glob("*.jsonl")):
        d = Counter(hook.audit(p))
        if d:
            n = hook.precomputed(p)
            per[p.stem[:8]] = {k.value: v for k, v in d.items()} | {"precomputed": n}
            total.update(d)
            ahead += n
    fires, errors, slowest = Counter(), 0, 0.0
    log = live.live_dir() / "hook.jsonl"
    for line in log.read_text().splitlines() if log.exists() else []:
        r = json.loads(line)
        fires[r.get("source", "")] += 1
        errors += "error" in r
        slowest = max(slowest, r.get("seconds", 0.0))
    lost = live.diverged(archive.connect(live.db_path())) if live.db_path().exists() else []
    out = {"root": str(root), "boundaries": sum(total.values()), "precomputed": ahead,
           **{k.value: total[k] for k in hook.Delivery},
           "firings": dict(fires), "errors": errors, "slowest_seconds": slowest, "live_diverged": len(lost)}
    if a.verbose:
        out["sessions"] = per
    print(json.dumps(out, ensure_ascii=False, indent=1))
    sys.exit(1 if total[hook.Delivery.MISSING] or total[hook.Delivery.LATE] or errors or lost else 0)


def cmd_live(a: argparse.Namespace) -> None:
    path = live.db_path()
    archive.check_space(path)
    db = archive.connect(path)
    t0 = time.monotonic()
    st = live.refresh(db, Path(a.root).expanduser(), block=True, sub=a.sub)
    print(json.dumps({"db": str(path), "files": st.files, "lines": st.lines, "skipped": st.skipped,
                      "diverged": len(live.diverged(db)),
                      "boundaries": st.boundaries, "kinds": dict(st.kinds), "redacted": dict(st.redacted),
                      "seconds": round(time.monotonic() - t0, 1), "db_bytes": path.stat().st_size}, ensure_ascii=False))


def cmd_selftest(a: argparse.Namespace) -> None:
    rep = selftest.run(a.model, tuple(map(selftest.Scenario, a.only or selftest.Scenario)), a.keep)
    print(json.dumps(rep, ensure_ascii=False, indent=1))
    sys.exit(0 if rep["ok"] else 1)


def cmd_doctor(a: argparse.Namespace) -> None:
    rep = doctor.run()
    print(json.dumps(rep, ensure_ascii=False, indent=1))
    sys.exit(0 if rep["ok"] else 1)


class QaAction(StrEnum):
    QUESTIONS = "questions"  # counts only, free
    CEILING = "ceiling"  # retrieval upper bound for S2, free
    RUN = "run"  # paid: claude -p per question and system
    REPORT = "report"


def per_gen(spec: str) -> dict[int, int]:
    return {int(g): int(n) for g, n in (x.split("=") for x in spec.split(",") if x)}


def gens(spec: str) -> tuple[int, ...]:
    return tuple(sorted({int(g) for g in spec.split(",") if g}))


def cmd_qa(a: argparse.Namespace) -> None:
    act, out = QaAction(a.action), Path(a.out).expanduser()
    if act == QaAction.REPORT:
        calls = qa.load_calls([out, *(Path(d).expanduser() for d in a.merge)])
        print(json.dumps(qa.report(calls), ensure_ascii=False, indent=1))
        return
    db, m = open_db(a), corpus.Manifest.load(Path(a.manifest))
    since = None if a.all else replay.recent_cutoff(m.created)
    exclude, only = tuple(x.lower() for x in a.exclude), qa.Stratum(a.only)
    tls = list(timelines(db, with_cuts=True))
    qs = qa.questions(tls, gens(a.gens), since, exclude)
    if act == QaAction.QUESTIONS:
        tag = qa.strata(tls, qs)
        hidden = [q for q in qs if tag[q] == qa.Stratum.HIDDEN]
        per = {g: {"n": sum(q.generation == g for q in qs), "hidden": sum(q.generation == g for q in hidden),
                   "hidden_sessions": len({q.session for q in hidden if q.generation == g})}
               for g in sorted({q.generation for q in qs})}
        print(json.dumps({"since": since, "registry": registry.params(), "questions": len(qs),
                          "sessions": len({q.session for q in qs}), "by_generation": per,
                          "distance": replay.quantiles([q.distance for q in qs])}, ensure_ascii=False))
        return
    if only != qa.Stratum.ALL:
        tag = qa.strata(tls, qs)
        qs = [q for q in qs if tag[q] == only]
    pick = qa.sample(qs, per_gen(a.per_gen))
    if act == QaAction.CEILING:
        print(json.dumps(qa.ceiling(db, pick, exclude=exclude), ensure_ascii=False, indent=1))
        return
    calls = qa.run(data_dir(a.data) / "archive.db", pick, [qa.System(x) for x in a.systems.split(",")], out,
                   a.model, a.max_calls, a.stop_at, exclude, Path(m.root), a.turns, qa.Effort(a.effort))
    print(json.dumps(qa.report(calls), ensure_ascii=False, indent=1))


def main() -> None:
    p = argparse.ArgumentParser(prog="eternal")
    p.add_argument("--data", help="archive directory (default $ETERNAL_DATA or ~/.local/share/eternal)")
    sub = p.add_subparsers(required=True)
    f = sub.add_parser("freeze")
    f.add_argument("--root", default="~/.claude/projects")
    f.add_argument("--sub", action="store_true", help="include subagent transcripts")
    f.add_argument("--exclude", nargs="*", default=[], help="session ids to leave out")
    f.add_argument("--out", required=True)
    f.set_defaults(fn=cmd_freeze)
    i = sub.add_parser("ingest")
    i.add_argument("--manifest", required=True)
    i.add_argument("-v", "--verbose", action="store_true")
    i.set_defaults(fn=cmd_ingest)
    b = sub.add_parser("bench")
    b.add_argument("--manifest", required=True)
    b.add_argument("--out", required=True)
    b.add_argument("--md")
    b.set_defaults(fn=cmd_bench)
    c = sub.add_parser("check")
    c.add_argument("--manifest", required=True)
    c.add_argument("--baseline", required=True)
    c.set_defaults(fn=cmd_check)
    s = sub.add_parser("search")
    s.add_argument("query")
    s.add_argument("-k", type=int, default=10)
    s.add_argument("--trigram", action="store_true")
    s.add_argument("--raw", action="store_true", help="pass FTS5 syntax through unquoted")
    s.set_defaults(fn=cmd_search)
    lv = sub.add_parser("live", help="append new lines of every transcript to the live archive")
    lv.add_argument("--root", default=str(live.ROOT))
    lv.add_argument("--sub", action="store_true", help="include subagent transcripts")
    lv.set_defaults(fn=cmd_live)
    au = sub.add_parser("audit", help="did the registry reach the model after each compaction")
    au.add_argument("--root", help="transcript directory (default: sessions started in this repo)")
    au.add_argument("-v", "--verbose", action="store_true")
    au.set_defaults(fn=cmd_audit)
    st = sub.add_parser("selftest", help="paid: a real session in this repo compacts, gets the registry, recalls")
    st.add_argument("--model", default="haiku")
    st.add_argument("--only", nargs="*", choices=[x.value for x in selftest.Scenario])
    st.add_argument("--keep", action="store_true", help="leave the test transcripts in the project")
    st.set_defaults(fn=cmd_selftest)
    dr = sub.add_parser("doctor", help="free: hook, MCP server, trust, memory, live archive, last selftest")
    dr.set_defaults(fn=cmd_doctor)
    q = sub.add_parser("qa", help="B5 (Tier-2): which file was written after an old prompt")
    q.add_argument("action", choices=[x.value for x in QaAction])
    q.add_argument("--manifest", default="baseline/manifest.json")
    q.add_argument("--all", action="store_true", help="all compactions, not only recent ones")
    q.add_argument("--exclude", nargs="*", default=["1cef4f2f"],
                   help="session prefixes kept out of questions and search")
    q.add_argument("--gens", default=",".join(map(str, qa.GENS)), help="compactions after the prompt, e.g. 1,3,10,20")
    q.add_argument("--only", choices=[x.value for x in qa.Stratum], default=qa.Stratum.ALL.value,
                   help="shown: the registry lists the prompt; hidden: thinned out of it")
    q.add_argument("--per-gen", default="1=50,3=50,10=50", help="questions per generation, e.g. 1=2,3=2,10=1")
    q.add_argument("--systems", default=",".join(qa.System))
    q.add_argument("--model", default="haiku")
    q.add_argument("--max-calls", type=int, default=15)
    q.add_argument("--turns", type=int, default=qa.TURNS, help="turn cap for the systems that use tools (S2-S4)")
    q.add_argument("--effort", choices=[x.value for x in qa.Effort], default=qa.EFFORT.value)
    q.add_argument("--stop-at", type=float, default=0.5, help="stop when a rate-limit window reaches this utilization")
    q.add_argument("--out", default=f"/var/tmp/{getpass.getuser()}-eternal-qa/pilot")
    q.add_argument("--merge", nargs="*", default=[], help="report: more run dirs, e.g. a rerun with another --turns")
    q.set_defaults(fn=cmd_qa)
    a = p.parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()
