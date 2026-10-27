"""Doctor: the configured hook and server run on synthetic data; static checks over a fake home."""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from eternal import archive, doctor, hook, live, selftest

from . import fixtures as fx

REPO = Path(__file__).resolve().parents[1]


def write_json(path: Path, d: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(d))


class Doctor(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name) / "home"
        self.repo = self.home / "projects/x"
        (self.repo / ".claude").mkdir(parents=True)
        self.c = doctor.Ctx(self.repo, self.home)
        self.env = mock.patch.dict(os.environ, {"ETERNAL_LIVE": str(Path(self.tmp.name) / "live")})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def test_configured_hook_and_server_run_on_synthetic_data(self):
        c = doctor.Ctx(REPO, self.home)
        self.assertTrue(doctor.check_hook(c)[0])
        self.assertEqual([doctor.check_hook_runs(c)[0], doctor.check_handshake(c)[0]], [True, True])
        self.assertFalse(live.db_path().exists())

    def test_hook_must_fire_on_compaction(self):
        hooks = [{"matcher": "startup", "hooks": [{"type": "command", "command": "python3 -m eternal.hook"}]}]
        write_json(self.repo / ".claude/settings.json", {"hooks": {"SessionStart": hooks}})
        self.assertIsNone(doctor.hook_command(self.repo))
        hooks[0]["matcher"] = "compact|resume"
        write_json(self.repo / ".claude/settings.json", {"hooks": {"SessionStart": hooks}})
        self.assertEqual(doctor.hook_command(self.repo), ("python3 -m eternal.hook", doctor.TIMEOUT))

    def test_trust_approval_memory_and_cleanup(self):
        write_json(self.home / ".claude.json", {"projects": {
            str(self.home): {"hasTrustDialogAccepted": True}, str(self.repo): {"enabledMcpjsonServers": ["eternal"]}}})
        write_json(self.home / ".claude/settings.json", {"cleanupPeriodDays": 30})
        self.assertEqual(doctor.check_trust(self.c), (True, f"accepted for {self.home}"))
        self.assertEqual(doctor.approval(self.c), (True, "enabled in ~/.claude.json"))
        self.assertFalse(doctor.check_kept(self.c)[0])
        write_json(self.repo / ".claude/settings.local.json", {"disabledMcpjsonServers": ["eternal"]})
        self.assertEqual(doctor.approval(self.c), (False, "disabled in .claude/settings.local.json"))
        self.assertFalse(doctor.check_memory(self.c)[0])
        main = live.project_dir(self.home, self.c.root) / "memory"
        main.mkdir(parents=True)
        mine = live.project_dir(self.repo, self.c.root) / "memory"
        mine.parent.mkdir(parents=True)
        mine.symlink_to(Path("..") / main.parent.name / "memory")
        self.assertTrue(doctor.check_memory(self.c)[0])
        (self.home / ".claude.json").write_text('{"projects": {')
        with mock.patch.object(doctor.time, "sleep"):
            ok, detail = doctor.check_trust(self.c)
        self.assertEqual((ok, detail.split(":")[0]), (True, "unknown"))

    def test_hook_log_counts_errors_of_the_current_code_only(self):
        log = live.live_dir() / "hook.jsonl"
        log.parent.mkdir(parents=True)
        rows = [{"source": "compact", "code": "old", "error": "boom"},
                {"source": "compact", "code": hook.code_digest(), "seconds": 0.5}]
        log.write_text("".join(json.dumps(r) + "\n" for r in rows))
        self.assertEqual(doctor.check_hook_log(self.c), (True, "1 firings of this code {'compact': 1}, slowest 0.5s"))
        with open(log, "a") as f:
            f.write(json.dumps({"source": "startup", "code": hook.code_digest(), "error": "ValueError: x"}) + "\n")
        ok, detail = doctor.check_hook_log(self.c)
        self.assertEqual((ok, detail.endswith("1 errors, last: ValueError: x")), (False, True))

    def test_selftest_must_cover_every_scenario_on_this_version(self):
        last = live.live_dir() / "selftest" / selftest.LAST
        both = {s.value: {"claude_code": "2.0.0"} for s in selftest.Scenario}
        with mock.patch.object(doctor, "claude_version", return_value="2.0.0"):
            self.assertFalse(doctor.check_selftest(self.c)[0])
            write_json(last, {"ok": True, "ts": "t", "scenarios": both})
            self.assertTrue(doctor.check_selftest(self.c)[0])
            write_json(last, {"ok": True, "ts": "t", "scenarios": {"recall": both["recall"]}})
            self.assertFalse(doctor.check_selftest(self.c)[0])
        write_json(last, {"ok": True, "ts": "t", "scenarios": both})
        with mock.patch.object(doctor, "claude_version", return_value="2.0.1"):
            self.assertFalse(doctor.check_selftest(self.c)[0])

    def test_live_archive_missing_behind_or_diverged(self):
        self.assertFalse(doctor.check_live(self.c)[0])
        path = fx.write(self.c.root / "-p/s1.jsonl", [fx.human("первый промпт про малахит")])
        db = archive.connect(live.db_path())
        try:
            live.refresh(db, self.c.root)
            self.assertEqual(doctor.check_live(self.c)[1].split(",")[:3],
                             ["1 transcripts", " 0 with lines a search has yet to take in", " 0 diverged"])
            fx.append(path, [fx.human("второй промпт про янтарь")])
            self.assertIn(" 1 with lines", doctor.check_live(self.c)[1])
            fx.write(path, [fx.human("короткий")])
            live.refresh(db, self.c.root)
            self.assertFalse(doctor.check_live(self.c)[0])
        finally:
            db.close()

    def test_run_reports_a_raising_check_as_failed(self):
        with mock.patch.dict(doctor.CHECKS, {doctor.Check.HOOK: mock.Mock(side_effect=OSError("disk"))}):
            rep = doctor.run(self.repo, self.home)
        self.assertEqual((rep["ok"], rep["checks"]["hook_configured"]), (False, {"ok": False, "detail": "OSError: disk"}))


if __name__ == "__main__":
    unittest.main()
