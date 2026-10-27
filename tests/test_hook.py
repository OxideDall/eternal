import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from eternal import hook

from . import fixtures as fx


class Hook(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = fx.write(Path(self.tmp.name) / "proj/s1.jsonl", [
            fx.human("первый промпт: собери реестр и проверь хук"),
            fx.tool_use("Write", {"file_path": "/home/u/p/registry.py", "content": "x"}, "m1", "t1"),
            fx.tool_result("t1", "ok"),
            fx.boundary(),
            fx.summary("Summary: ..."),
            fx.human("второй промпт с ключом sk-ant-" + "a" * 30),
            fx.boundary(),
        ], tail='{"type": "user", "partial')

        self.live = Path(self.tmp.name) / "live"

    def tearDown(self):
        self.tmp.cleanup()

    def handle(self, inp: dict) -> tuple[dict | None, dict]:
        return hook.handle(inp, self.live)

    def inp(self, source: str = "compact") -> dict:
        return {"session_id": "abcdef12-3456", "transcript_path": str(self.path), "cwd": "/x",
                "hook_event_name": "SessionStart", "source": source}

    def test_compact_injects_redacted_registry(self):
        out, rec = self.handle(self.inp())
        ctx = out["hookSpecificOutput"]["additionalContext"]
        self.assertEqual(out["hookSpecificOutput"]["hookEventName"], "SessionStart")
        self.assertIn("L0 10-01 первый промпт", ctx)
        self.assertIn("L1 10-01 ~/p/registry.py", ctx)
        self.assertIn("[redacted:anthropic]", ctx)
        self.assertNotIn("sk-ant-", ctx)
        self.assertEqual((rec["lines"], rec["boundaries"], rec["summarized"], rec["prompts"], rec["shown_files"]),
                         (7, 2, False, 2, 1))
        self.assertEqual(rec["code"], hook.code_digest())

    def test_other_sources_only_log(self):
        out, rec = self.handle(self.inp("startup"))
        self.assertIsNone(out)
        self.assertEqual((rec["source"], "boundaries" in rec), ("startup", False))

    def test_startup_before_transcript_exists(self):
        out, rec = self.handle({**self.inp("startup"), "transcript_path": f"{self.path}.new"})
        self.assertEqual((out, rec["bytes"], "error" in rec), (None, 0, False))

    def test_errors_are_logged_not_raised(self):
        out, rec = self.handle({**self.inp(), "transcript_path": f"{self.path}.missing"})
        self.assertEqual((out, rec["error"].split(":")[0]), (None, "FileNotFoundError"))
        self.assertIn("ValueError", self.handle({**self.inp(), "source": "weird"})[1]["error"])

    def test_main_prints_json_and_logs(self):
        live, buf = Path(self.tmp.name) / "live", io.StringIO()
        with (mock.patch.dict("os.environ", {"ETERNAL_LIVE": str(live)}),
              mock.patch("sys.stdin", io.StringIO(json.dumps(self.inp()))), redirect_stdout(buf)):
            hook.main()
        self.assertIn("additionalContext", json.loads(buf.getvalue())["hookSpecificOutput"])
        (rec,) = [json.loads(x) for x in (live / "hook.jsonl").read_text().splitlines()]
        self.assertEqual((rec["session"], rec["event"]), ("abcdef12", "SessionStart"))
        self.assertTrue(hook.cache_file(live, self.path).exists())

    def test_split_scan_equals_full_scan(self):
        """Continuing a scan from any split, after a boundary or inside a line, equals one full scan."""
        full, whole = self.path.read_bytes(), hook.scan(self.path)
        ends = [i + 1 for i, b in enumerate(full) if b == ord("\n")]
        part = Path(self.tmp.name) / "part.jsonl"
        for cut in [0, *ends, ends[2] + 7]:
            part.write_bytes(full[:cut])
            self.assertEqual(hook.scan(self.path, hook.scan(part)), whole, cut)
        self.assertEqual((whole.offset, whole.lines), (ends[-1], 7))

    def test_cache_is_reused_and_voided(self):
        """A later compaction reuses cached lines; a rewritten tail, truncation or other code rescans from 0."""
        path = fx.write(Path(self.tmp.name) / "proj/s2.jsonl", [
            fx.human("первый промпт: собери реестр и проверь хук"), fx.boundary(), fx.summary("S")])
        inp = {**self.inp(), "transcript_path": str(path)}

        def reused() -> int:
            return self.handle(inp)[1]["reused_lines"]

        self.assertEqual(reused(), 0)
        fx.append(path, [fx.human("второй промпт: допиши кэш скана в хук"), fx.boundary()])
        out, rec = self.handle(inp)
        self.assertEqual((rec["reused_lines"], rec["lines"], out), (3, 5, hook.handle(inp, self.live / "cold")[0]))
        cache = hook.cache_file(self.live, path)
        self.assertEqual((cache.stat().st_mode & 0o777, cache.parent.stat().st_mode & 0o777), (0o600, 0o700))
        path.write_text(path.read_text().replace("первый", "ПЕРВЫЙ"))  # same length, new bytes
        out, rec = self.handle(inp)
        self.assertEqual(rec["reused_lines"], 0)
        self.assertIn("ПЕРВЫЙ промпт", out["hookSpecificOutput"]["additionalContext"])
        self.assertEqual(reused(), 5)
        with mock.patch.object(hook, "code_digest", return_value="other"):
            self.assertEqual(reused(), 0)
        self.assertEqual(reused(), 0)
        fx.write(path, [fx.human("первый промпт: собери реестр и проверь хук")])
        self.assertEqual((reused(), self.handle(inp)[1]["lines"]), (0, 1))
        self.assertEqual(reused(), 1)


class Audit(unittest.TestCase):
    def test_delivery_per_boundary(self):
        reg = fx.hook_context("[eternal registry] Session x: ...")
        with tempfile.TemporaryDirectory() as d:
            path = fx.write(Path(d) / "s.jsonl", [
                fx.human("старт"), fx.boundary(), fx.summary("S"), reg, fx.text("a", "m1"),  # delivered
                fx.boundary(), fx.text("b", "m2"), fx.hook_context("[eternal registry] late"),  # late
                fx.boundary(), fx.hook_context("other hook text"), fx.text("c", "m3"),  # missing
                fx.boundary(), fx.summary("S")])  # pending
            self.assertEqual(hook.audit(path), [hook.Delivery.DELIVERED, hook.Delivery.LATE, hook.Delivery.MISSING,
                                                hook.Delivery.PENDING])

    def test_precomputed_boundaries_counted(self):
        with tempfile.TemporaryDirectory() as d:
            path = fx.write(Path(d) / "s.jsonl", [
                fx.human("старт"), fx.boundary(precomputed=True), fx.summary("S"), fx.text("a", "m1"),
                fx.boundary(), fx.human("precomputed compact_boundary в тексте промпта")])
            self.assertEqual((hook.precomputed(path), len(hook.audit(path))), (1, 2))

    def test_registry_mark_matches_header(self):
        self.assertTrue(hook.registry.HEADER.startswith(hook.MARK))


if __name__ == "__main__":
    unittest.main()
