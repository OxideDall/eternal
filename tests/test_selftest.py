"""Selftest plumbing without a model: env, checks over synthetic events, the driver, the report."""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from eternal import hook, live, selftest

from . import fixtures as fx

SID = "5e1f7e57-0000-4000-8000-000000000001"


def init(**kw) -> dict:
    return {"type": "system", "subtype": "init", "session_id": SID, "claude_code_version": "9.9.9", **kw}


def call(name: str) -> dict:
    return {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": name, "input": {}}]}}


RESULT = {"type": "result", "result": "ок", "total_cost_usd": 0.01}
BOUNDARY = {"type": "system", "subtype": "compact_boundary"}


class Selftest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name) / "home"
        self.env = mock.patch.dict(os.environ, {"HOME": str(self.home), "ETERNAL_LIVE": str(Path(self.tmp.name) / "live")})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def test_fresh_env_drops_what_a_session_exports(self):
        with mock.patch.dict(os.environ, {"CLAUDECODE": "1", "CLAUDE_CODE_SESSION_ID": "x", "TUNE_MEMORY_DIR": "/m",
                                          "CLAUDE_COWORK_MEMORY_PATH_OVERRIDE": "/m", "CLAUDE_CONFIG_DIR": "/c"}):
            env = selftest.fresh_env(EXTRA="1")
        self.assertFalse({"CLAUDECODE", "CLAUDE_CODE_SESSION_ID", "TUNE_MEMORY_DIR", "CLAUDE_COWORK_MEMORY_PATH_OVERRIDE"}
                         & set(env))
        self.assertEqual((env["CLAUDE_CONFIG_DIR"], env["EXTRA"], env["HOME"]), ("/c", "1", str(self.home)))

    def test_inside_counts_boundaries_after_a_tool_call_of_the_turn(self):
        ev = [init(), RESULT, BOUNDARY, call("Read"), BOUNDARY, RESULT, BOUNDARY, RESULT]
        self.assertEqual((selftest.inside(ev, 1), selftest.inside(ev, 0), selftest.inside(ev, 2), selftest.inside(ev, 3)),
                         ([4], [], [], []))

    def test_common_passes_on_a_delivered_compaction_and_fails_on_separate_memory(self):
        fx.write(self.home / f".claude/projects/-x/{SID}.jsonl", [
            fx.human("старт"), fx.boundary(), fx.summary("S"), fx.hook_context("[eternal registry] Session x"),
            fx.text("a", "m1")])
        log = live.live_dir() / "hook.jsonl"
        log.parent.mkdir(parents=True)
        log.write_text(json.dumps({"session": SID[:8], "source": "compact", "chars": 900, "seconds": 0.01}) + "\n")
        servers = [{"name": "eternal", "status": "connected", "source": "project"}]
        ev = [init(mcp_servers=servers, memory_paths={"auto": f"{selftest.main_memory()}/"}), RESULT, BOUNDARY, RESULT]
        checks = {}
        selftest.common(ev, checks, 2)
        self.assertEqual({k: v["ok"] for k, v in checks.items()},
                         {c.value: True for c in (selftest.Check.RAN, selftest.Check.MCP, selftest.Check.COMPACTED,
                                                   selftest.Check.MEMORY, selftest.Check.HOOK, selftest.Check.DELIVERED)})
        ev[0]["memory_paths"] = {"auto": str(self.home / ".claude/projects/-x/memory")}
        selftest.common(ev + [{"type": "selftest_error", "error": "boom"}], checks, 2)
        self.assertEqual((checks["shared_memory"]["ok"], checks["ran"]["ok"]), (False, False))

    def test_drive_returns_events_when_the_process_dies_or_stalls(self):
        log = Path(self.tmp.name) / "drive.jsonl"
        once = "import json,sys; sys.stdin.readline(); print(json.dumps({'type': 'result'}), flush=True)"
        ev = selftest.drive([sys.executable, "-c", once], ["a", "b"], log)
        self.assertEqual([e["type"] for e in ev], ["result", "selftest_error"])
        self.assertEqual(log.stat().st_mode & 0o777, 0o600)
        ev = selftest.drive([sys.executable, "-c", "pass"], ["a" * (1 << 20), "b"], log)
        self.assertEqual([e["type"] for e in ev], ["selftest_error"])
        with mock.patch.object(selftest, "TURN_TIMEOUT", 0.3):
            ev = selftest.drive([sys.executable, "-c", "import sys; sys.stdin.read()"], ["a"], log)
        self.assertIn("TimeoutError", ev[-1]["error"])

    def test_run_hides_the_session_stashes_its_transcript_and_writes_last(self):
        path = fx.write(self.home / f".claude/projects/-repo/{SID}.jsonl", [fx.human("старт")])
        hook.save_scan(hook.cache_file(live.live_dir(), path), path, hook.scan(path))

        def fake(model, work, out, checks):
            selftest.put(checks, selftest.Check.RAN, True, "")
            return [[init(), RESULT]]

        with mock.patch.dict(selftest.SCENARIOS, {s: fake for s in selftest.Scenario}):
            rep = selftest.run(scenarios=(selftest.Scenario.RECALL,))
        moved = live.live_dir() / "selftest" / path.name
        self.assertEqual((rep["ok"], rep["cost_usd"], rep["scenarios"]["recall"]["claude_code"]), (True, 0.01, "9.9.9"))
        self.assertEqual((path.exists(), moved.exists(), hook.cache_file(live.live_dir(), path).exists()),
                         (False, True, False))
        self.assertEqual(live.excluded(), (SID,))
        last = live.live_dir() / "selftest" / selftest.LAST
        self.assertEqual((json.loads(last.read_text())["ok"], last.stat().st_mode & 0o777), (True, 0o600))
        self.assertFalse(selftest.run(scenarios=())["ok"])


if __name__ == "__main__":
    unittest.main()
