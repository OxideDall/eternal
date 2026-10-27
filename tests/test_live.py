"""Live archive: incremental refresh of growing transcripts under one lock."""
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from eternal import archive, corpus, live

from . import fixtures as fx

REL = "-home-u-p/aaaa1111.jsonl"


class Refresh(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.env = mock.patch.dict(os.environ, {"ETERNAL_LIVE": str(base / "live")})
        self.env.start()
        self.root = base / "projects"
        self.path = fx.write(self.root / REL, [fx.human("первый промпт про малахит"), fx.text("ответ", "m1")])
        self.db = archive.connect(live.db_path())

    def tearDown(self):
        self.db.close()
        self.env.stop()
        self.tmp.cleanup()

    def lines(self, word: str) -> list[int]:
        return [h.line for h in archive.search(self.db, archive.quote(word))]

    def test_appends_complete_lines_only(self):
        self.assertEqual(live.refresh(self.db, self.root).files, 1)
        fx.append(self.path, [fx.human("второй промпт про янтарь")])
        with open(self.path, "a") as f:
            f.write('{"type": "user", "mess')
        st = live.refresh(self.db, self.root)
        self.assertEqual((st.files, st.lines), (1, 1))
        self.assertEqual(self.lines("янтарь"), [2])
        self.assertEqual(live.refresh(self.db, self.root).files, 0)

    def test_unchanged_files_are_not_read(self):
        live.refresh(self.db, self.root)
        with mock.patch.object(corpus, "_last_newline", side_effect=AssertionError("read")):
            self.assertEqual(live.refresh(self.db, self.root).files, 0)

    def test_rewritten_or_truncated_file_is_skipped(self):
        live.refresh(self.db, self.root)
        self.path.write_text(self.path.read_text().replace("ответ", "ОТВЕТ"))
        fx.append(self.path, [fx.human("третий промпт после правки")])
        st = live.refresh(self.db, self.root)
        self.assertEqual((st.files, st.skipped), (0, [REL]))
        fx.write(self.path, [fx.human("короткий")])
        self.assertEqual(live.refresh(self.db, self.root).skipped, [REL])
        self.assertEqual(self.lines("третий"), [])

    def test_own_tool_calls_are_stored_not_indexed(self):
        fx.append(self.path, [fx.tool_use("mcp__eternal__search", {"query": "малахит"}, "m2", "t1")])
        live.refresh(self.db, self.root)
        fx.append(self.path, [fx.tool_result("t1", "1 hits: L0 первый промпт про малахит"),
                              fx.tool_use("Read", {"file_path": "/x/малахит.md"}, "m3", "t2")])
        live.refresh(self.db, self.root)
        self.assertEqual(self.lines("малахит"), [0, 4])
        kinds = [k for (k,) in self.db.execute("SELECT kind FROM record ORDER BY line")]
        self.assertEqual(kinds, ["human", "assistant_text", "tool_use", "tool_result", "tool_use"])

    def test_diverged_is_remembered(self):
        live.refresh(self.db, self.root)
        self.assertEqual(live.diverged(self.db), [])
        fx.write(self.path, [fx.human("короткий")])
        live.refresh(self.db, self.root)
        live.refresh(self.db, self.root)
        self.assertEqual(live.diverged(self.db), [REL])

    def test_busy_lock_returns_none(self):
        with live._lock(True) as got:
            self.assertTrue(got)
            self.assertIsNone(live.refresh(self.db, self.root))

    def test_exclusions_are_unique_lowercase_and_private(self):
        self.assertEqual(live.excluded(), ())
        live.exclude(["AB12", "", "cd34"])
        live.exclude(["ab12", "cd34", "ef56"])
        self.assertEqual(live.excluded(), ("ab12", "cd34", "ef56"))
        self.assertEqual((live.live_dir() / live.EXCLUDED).stat().st_mode & 0o777, 0o600)

    def test_prefix_archive_refuses_tail(self):
        archive.ingest(self.db, corpus.freeze(self.root))
        with self.assertRaises(ValueError):
            live.refresh(self.db, self.root)


if __name__ == "__main__":
    unittest.main()
