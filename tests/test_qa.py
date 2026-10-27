"""B5 harness: question generation, scoring, prompt assembly, and the resumable capped runner on a fake wrapper."""
import json
import os
import sqlite3
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from eternal import archive, corpus, registry
from eternal.bench import qa
from eternal.bench.timeline import timelines

from . import fixtures as fx

SID = "beef0000-0000-0000-0000-000000000001"
FAKE = """#!/usr/bin/env python3
import json, sys
sys.stdin.read()
print(json.dumps({"type": "assistant", "message": {"model": "claude-haiku-5-5", "content": [
    {"type": "tool_use", "name": "mcp__eternal__search", "input": {}}]}}))
print(json.dumps({"type": "rate_limit_event", "rate_limit_info": {"unifiedWindows": {
    "five_hour": {"utilization": %s}, "seven_day": {"utilization": 0.01}}}}))
print(json.dumps({"type": "result", "result": "parser_core.py", "total_cost_usd": 0.004, "num_turns": 2,
                  "usage": {"input_tokens": 3, "output_tokens": 9, "service_tier": "x"}}))
"""


BROKEN = """#!/usr/bin/env python3
import json, sys
sys.stdin.read()
print(json.dumps({"type": "system", "subtype": "api_retry"}))
print(%r)
"""
API_ERROR = {"type": "result", "is_error": True, "subtype": "success", "result": "API Error: Connection refused",
             "total_cost_usd": 0, "num_turns": 1}
OUT_OF_TURNS = {"type": "result", "is_error": True, "subtype": "error_max_turns", "total_cost_usd": 0.01,
                "num_turns": 9}


def session(extra: list[dict] = (), footer: str = "") -> list[dict]:
    return [
        fx.human("сначала разберём формат логов в проекте"),
        fx.tool_use("Read", {"file_path": "/p/a.py"}, "m1", "t1"), fx.tool_result("t1", "a"),
        fx.tool_use("Write", {"file_path": "/p/src/parser_core.py", "content": "x"}, "m2", "t2"),
        fx.tool_result("t2", "ok"),
        fx.human("теперь поправь readme и настройки проекта"),
        fx.tool_use("Write", {"file_path": "/p/README.md", "content": "x"}, "m3", "t3"), fx.tool_result("t3", "ok"),
        fx.human("добавь тест для report_view пожалуйста"),
        fx.tool_use("Write", {"file_path": "/p/tests/report_view.py", "content": "x"}, "m4", "t4"),
        fx.tool_result("t4", "ok"), *extra,
        fx.boundary(), fx.summary("Summary: работали над парсером" + footer),
        fx.human("второй эпохи промпт: напиши отчёт о замерах"),
        fx.tool_use("Write", {"file_path": "/p/measure.md", "content": "x"}, "m5", "t5"), fx.tool_result("t5", "ok"),
        fx.boundary(), fx.summary("Summary: отчёт о замерах написан" + footer)]


class Harness(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.db_path = self.base / "archive.db"
        self.box = self.base / "box"
        self.box.mkdir()
        sandbox = mock.patch.object(qa, "SANDBOX", self.box)
        sandbox.start()
        self.addCleanup(sandbox.stop)

    def tearDown(self):
        self.tmp.cleanup()

    def build(self, entries: list[dict]) -> sqlite3.Connection:
        root = self.base / "projects"
        fx.write(root / f"-home-u-p/{SID}.jsonl", entries)
        db = archive.connect(self.db_path)
        self.addCleanup(db.close)
        archive.ingest(db, corpus.freeze(root))
        return db

    def qs(self, entries: list[dict]) -> list[qa.Question]:
        return qa.questions(timelines(self.build(entries), with_cuts=True), (1, 3))

    def test_questions_skip_generic_named_and_missing_generations(self):
        [q] = self.qs(session())
        self.assertEqual((q.line, q.golden, q.distance, q.generation, q.epoch), (0, "parser_core.py", 3, 1, 0))
        self.assertIn("«сначала разберём формат логов в проекте»", q.text())

    def test_ambiguous_basename_is_dropped(self):
        other = [fx.tool_use("Edit", {"file_path": "/q/parser_core.py"}, "m9", "t9"), fx.tool_result("t9", "ok")]
        self.assertEqual(self.qs(session(other)), [])

    def test_verdict(self):
        V = qa.Verdict
        for answer, golden, want in (("parser_core.py", "parser_core.py", V.CORRECT),
                                     ("Файл: src/Parser_Core.py (beef0000:L3)", "parser_core.py", V.CORRECT),
                                     ("UNCLEAR", "parser_core.py", V.UNCLEAR), ("  ", "a.py", V.UNCLEAR),
                                     ("parser_core.py или a.py", "parser_core.py", V.WRONG),
                                     ("report.md", "parser_core.py", V.WRONG),
                                     ("claude-wrapped", "claude-wrapped", V.CORRECT),
                                     ("claude-wrapped.sh", "claude-wrapped", V.WRONG)):
            self.assertEqual(qa.verdict(answer, golden), want, answer)

    def test_prompt_carries_registry_only_in_s1_s2(self):
        db = self.build(session())
        [q] = qa.questions(timelines(db, with_cuts=True), (1,))
        [tl] = list(timelines(db, with_cuts=True))
        reg = qa.registry_text(tl, q)
        self.assertIn("L3 10-01 /p/src/parser_core.py", reg)
        self.assertEqual(q.cut_line, 16)  # +1 is the second compaction after the prompt, as in B1
        self.assertIn("L13 10-01 второй эпохи промпт: напиши отчёт о замерах → measure.md", reg)
        s0, s1, s2, s3, s4 = (qa.prompt(x, qa.summary_text(db, q), reg, q) for x in qa.System)
        self.assertTrue(s0.startswith("Summary: отчёт о замерах написан") and "[eternal registry]" not in s0)
        self.assertEqual(s0, s3)
        self.assertEqual(s0, s4)
        self.assertEqual(s1, s2)
        self.assertIn("SessionStart:compact hook additional context: [eternal registry]", s1)
        self.assertIn("L0 10-01 сначала разберём формат логов в проекте → parser_core.py", s1)
        cfg = Path("c.json")
        av = {x: qa.argv(x, "haiku", cfg) for x in qa.System}
        self.assertEqual(["--mcp-config" in a for a in av.values()], [False, False, True, True, False])
        self.assertTrue(all("--restricted" in a and a[a.index("--permission-prompts") + 1] == "none"
                            for a in av.values()))
        tools = {x: a[a.index("--tools") + 1] for x, a in av.items()}
        self.assertEqual(tools, {qa.System.S0: "", qa.System.S1: "", qa.System.S2: "", qa.System.S3: "",
                                 qa.System.S4: "Read,Grep"})
        turns = {x: a[a.index("--max-turns") + 1] for x, a in av.items()}
        self.assertEqual(list(turns.values()), ["2", "2", "8", "8", "8"])
        self.assertEqual(qa.argv(qa.System.S4, "haiku", None, 20)[-1], "20")
        self.assertTrue(all(a[a.index("--effort") + 1] == "low" for a in av.values()))
        hi = qa.argv(qa.System.S3, "haiku", cfg, 20, qa.Effort.MAX)
        self.assertEqual((hi[hi.index("--effort") + 1], hi[-1]), ("max", "20"))

    def test_listed_reads_only_the_prompt_section(self):
        reg = "\n".join(["[eternal registry] Session x:", "Prompts (2 of 9):", "L5 10-01 сделай (срочно):",
                         "L9 10-01 ещё", "Files written (1 of 1):", "L7 10-01 /p/a.py"])
        self.assertEqual([qa.listed(reg, n) for n in (5, 9, 7, 6)], [True, True, False, False])
        db = self.build(session())
        tls = list(timelines(db, with_cuts=True))
        qs = qa.questions(tls, (1,))
        self.assertEqual(list(qa.strata(tls, qs).values()), [qa.Stratum.SHOWN])
        with mock.patch.object(qa, "registry_text", lambda tl, q: "Prompts (0 of 1):"):
            self.assertEqual(list(qa.strata(tls, qs).values()), [qa.Stratum.HIDDEN])

    def test_sample_is_deterministic_and_spreads_sessions(self):
        qs = [qa.Question(f"s{i % 3}", "p", 1, 0, i, "x" * 30, "a.py", "/a.py", 1, 1 + i % 2, 9, "t", None)
              for i in range(12)]
        a = qa.sample(qs, {1: 3, 2: 1})
        self.assertEqual(a, qa.sample(list(reversed(qs)), {1: 3, 2: 1}))
        self.assertEqual(sorted(q.generation for q in a), [1, 1, 1, 2])
        self.assertEqual(len({q.session for q in a}), 3)  # every session before any repeats

    def run_fake(self, out: Path, utilization: float, script: str = "", **kw) -> list[qa.Call]:
        fake = self.base / "fake-wrapper"
        fake.write_text(script or FAKE % utilization)
        fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
        if not hasattr(self, "fake_qs"):  # fixtures mint fresh uids, so a rebuilt transcript would differ
            self.fake_qs = qa.questions(timelines(self.build(session()), with_cuts=True), (1,))
        with mock.patch.object(qa, "WRAPPER", fake):
            calls = qa.run(self.db_path, self.fake_qs, list(qa.System), out, root=self.base / "projects", **kw)
        self.assertEqual(list(self.box.iterdir()), [])  # every call's dir is gone
        return calls

    def test_run_resumes_and_respects_caps(self):
        out = self.base / "qa"
        calls = self.run_fake(out, 0.1, max_calls=2)
        self.assertEqual([c["system"] for c in calls], ["S0", "S1"])
        c = calls[0]
        self.assertEqual((c["answer"], c["verdict"], c["turns"], c["limits"]["five_hour"]),
                         ("parser_core.py", "correct", 2, 0.1))
        self.assertEqual(c["usage"], {"input_tokens": 3, "output_tokens": 9})
        calls = self.run_fake(out, 0.1, max_calls=5)
        self.assertEqual([c["system"] for c in calls], ["S0", "S1", "S2", "S3", "S4"])
        cfg = json.loads((out / f"{calls[2]['key']}.mcp.json").read_text())["mcpServers"]["eternal"]["env"]
        self.assertEqual(cfg["ETERNAL_ARCHIVE"], str(self.db_path))
        self.assertEqual(oct((out / "calls.jsonl").stat().st_mode & 0o777), "0o600")
        self.assertTrue(all(c["shown"] and c["registry"] == registry.params() for c in calls))
        rep = qa.report(calls + [{"key": "old", "system": "S1", "generation": 1, "verdict": "wrong"}])
        self.assertEqual(rep["calls"], 6)
        self.assertEqual(rep["systems"]["S2"]["shown"]["by_generation"][1]["correct"], 1)
        self.assertEqual(rep["systems"]["S1"]["all"]["by_generation"][1]["wrong"], 1)
        self.assertEqual(rep["registry_formats"], [registry.params(), None])

    def test_s4_reads_a_cut_copy_in_a_fresh_dir(self):
        src = self.base / "projects" / f"-home-u-p/{SID}.jsonl"
        db = self.build(session(footer=f"\nread the full transcript at: {src}"))
        [q] = qa.questions(timelines(db, with_cuts=True), (1,))
        seen = {}

        def fake_run(a, input, cwd, **kw):
            box = Path(cwd)
            seen.update(argv=a, files=sorted(x.name for x in box.iterdir()), box=box, input=input,
                        copy=(box / src.name).read_text())
            out = {"type": "result", "result": "parser_core.py", "num_turns": 3,
                   "permission_denials": [{"tool_name": "Read", "tool_input": {"file_path": "/home/u/x"}}]}
            return subprocess.CompletedProcess(a, 0, json.dumps(out), "")

        with mock.patch.object(qa.subprocess, "run", fake_run):
            [c] = qa.run(self.db_path, [q], [qa.System.S4], self.base / "qa4", root=self.base / "projects")
        copy = seen["box"] / src.name
        self.assertEqual((seen["files"], seen["box"].parent), ([src.name], self.box))
        self.assertEqual(len(seen["copy"].splitlines()), q.cut_line)
        self.assertNotIn(str(src), seen["copy"])
        self.assertIn(str(copy), seen["copy"])  # the earlier summary's footer, rewritten
        self.assertIn(f"read the full transcript at: {copy}", seen["input"])
        self.assertNotIn("[eternal registry]", seen["input"])
        self.assertEqual(list(self.box.iterdir()), [])
        self.assertEqual((c["denied"], c["verdict"]), (["/home/u/x"], "correct"))
        self.assertEqual(qa.report([c])["systems"]["S4"]["shown"]["by_generation"][1],
                         {"denied": 1, "turns": 3, "errors": 0, "out_of_turns": 0})  # refused: not scored
        miss = qa.report([{"key": "k", "system": "S4", "generation": 1, "verdict": "unclear", "tool_calls": []}])
        self.assertEqual(miss["systems"]["S4"]["all"]["by_generation"][1]["failed_no_tools"], 1)
        with self.assertRaises(ValueError):
            qa.run(self.db_path, [q], [qa.System.S4], self.base / "qa5")

    def test_s4_skips_a_summary_pointing_elsewhere(self):
        db = self.build(session(footer="\nread the full transcript at: /x/other-session.jsonl"))
        [q] = qa.questions(timelines(db, with_cuts=True), (1,))
        with mock.patch.object(qa.subprocess, "run", side_effect=AssertionError("must not be asked")):
            calls = qa.run(self.db_path, [q], [qa.System.S4], self.base / "qa6", root=self.base / "projects")
        self.assertEqual(calls[0]["skipped"], "footer")
        self.assertEqual(qa.report(calls)["systems"]["S4"]["shown"]["by_generation"][1], {"skipped": 1})
        again = qa.run(self.db_path, [q], [qa.System.S4], self.base / "qa6", root=self.base / "projects")
        self.assertEqual(len(again), 1)  # resumed, not re-asked

    def test_copy_rewrites_the_tilde_form_too(self):
        src = self.base / "home/.claude/projects/-p/s.jsonl"
        src.parent.mkdir(parents=True)
        src.write_text("a ~/.claude/projects/-p/s.jsonl\nb %s\nc\n" % src)
        dst = self.base / "copy.jsonl"
        with mock.patch.dict(os.environ, {"HOME": str(self.base / "home")}):
            qa.transcript_copy(src, 2, dst)
        self.assertEqual(dst.read_text(), f"a {dst}\nb {dst}\n")

    def test_pairs_count_who_alone_was_right(self):
        def call(key, system, verdict, **kw):
            return {"key": key, "system": system, "generation": 1, "verdict": verdict, "shown": False, **kw}
        calls = [call("a", "S2", "correct"), call("a", "S4", "wrong"), call("b", "S2", "unclear"),
                 call("b", "S4", "correct"), call("c", "S2", "correct"), call("c", "S4", "correct", denied=["/x"]),
                 call("d", "S2", "wrong"), call("d", "S4", "wrong")]
        self.assertEqual(qa.report(calls)["pairs"], {"hidden": {"S2-S4": {
            "S2": 1, "S4": 1, "neither": 1, "gave_up_S4": 1, "gave_up_S2": 1, "gave_up_both": 1}}})  # no tool calls

    def test_report_merges_runs_by_turn_cap(self):
        def call(system, cap, verdict, tools=("Grep",), effort=None):
            return {"key": "k", "system": system, "generation": 1, "verdict": verdict, "shown": True,
                    "argv": ["-p", "--max-turns", str(cap)] + (["--effort", effort] if effort else []),
                    "tool_calls": list(tools), "turns": 4, "cost_usd": 0.01}
        runs = [self.base / "r1", self.base / "r2", self.base / "r3"]
        logs = ([call("S3", 8, "correct"), call("S4", 8, "unclear", ()), call("S0", 2, "unclear", ())],
                [call("S4", 20, "correct")],
                [call("S4", 20, "correct", effort="max"), call("S0", 2, "unclear", (), "max"),
                 call("S3", 8, "correct", effort="low")])
        for d, cs in zip(runs, logs):
            d.mkdir()
            (d / "calls.jsonl").write_text("".join(json.dumps(c) + "\n" for c in cs))
        with self.assertRaises(ValueError):  # the explicit default effort is the same label as none
            qa.load_calls(runs)
        (runs[2] / "calls.jsonl").write_text("".join(json.dumps(c) + "\n" for c in logs[2][:2]))
        rep = qa.report(qa.load_calls(runs))
        self.assertEqual(sorted(rep["systems"]), ["S0", "S0/max", "S3", "S4", "S4@20", "S4@20/max"])
        runs = runs[:2]
        rep = qa.report(qa.load_calls(runs))
        self.assertEqual(sorted(rep["systems"]), ["S0", "S3", "S4", "S4@20"])
        self.assertEqual(rep["pairs"]["shown"]["S4-S4@20"], {"S4@20": 1, "gave_up_S4": 1})
        t = rep["systems"]["S4@20"]["shown"]["total"]
        self.assertEqual((t["tried_correct"], t["answer_turns"], t["cost_per_call"]),
                         (1, {"mean": 4.0, "max": 4}, 0.01))
        self.assertEqual(rep["systems"]["S3"]["shown"]["total"]["ci95"], [0.207, 1.0])
        with self.assertRaises(ValueError):
            qa.load_calls(runs + [runs[1]])

    def test_failures_outside_the_model_are_retried_not_scored(self):
        out = self.base / "qa7"
        self.assertEqual(self.run_fake(out, 0.1, BROKEN % json.dumps(API_ERROR), max_calls=10), [])
        failed = [json.loads(x) for x in (out / "failed.jsonl").read_text().splitlines()]
        self.assertEqual([c["system"] for c in failed], ["S0", "S1", "S2"])  # stopped after MAX_FAILS in a row
        self.assertTrue((out / f"{failed[0]['key']}.S0.failed.jsonl").exists())
        self.assertEqual(failed[0]["error"], "api: API Error: Connection refused")
        self.assertFalse((out / "calls.jsonl").exists())
        calls = self.run_fake(out, 0.1, max_calls=10)
        self.assertEqual([c.get("skipped") or c["verdict"] for c in calls], ["correct"] * 4 + ["footer"])

    def test_running_out_of_turns_is_scored(self):
        calls = self.run_fake(self.base / "qa8", 0.1, BROKEN % json.dumps(OUT_OF_TURNS), max_calls=1)
        self.assertEqual((calls[0]["error"], calls[0]["verdict"]), ("error_max_turns", "unclear"))

    def test_run_stops_at_utilization(self):
        calls = self.run_fake(self.base / "qa2", 0.9, max_calls=5, stop_at=0.5)
        self.assertEqual(len(calls), 1)


if __name__ == "__main__":
    unittest.main()
