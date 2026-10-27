import tempfile
import unittest
from collections import Counter
from pathlib import Path

from eternal import archive, corpus
from eternal.kinds import Kind

from . import fixtures as fx


class Ingest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "projects"
        self.p1 = fx.human("Починили курсор в memory.ts, ключ sk-ant-abcdefghijklmnopqrstuv", uuid="h1")
        self.path = fx.write(self.root / "proj/s1.jsonl", [
            self.p1,
            fx.text("часть 1", "m1"),
            fx.text("часть 2", "m1", {"input_tokens": 1, "cache_read_input_tokens": 100, "output_tokens": 50}),
            fx.boundary(("h1",)),
            fx.summary("Summary: 6. All user messages: - починили курсор"),
            fx.human("второй промпт после компакта"),
        ])
        self.db = archive.connect(Path(self.tmp.name) / "data/archive.db")

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def test_epochs_summary_link_and_dedup(self):
        st = archive.ingest(self.db, corpus.freeze(self.root))
        self.assertEqual((st.files, st.boundaries, st.unsummarized), (1, 1, 0))
        rows = list(self.db.execute("SELECT kind, epoch FROM record ORDER BY line, block"))
        self.assertEqual(rows, [("human", 0), ("assistant_text", 0), ("assistant_text", 0),
                                ("compact_summary", 1), ("human", 1)])
        epoch, summary_kind, preserved = self.db.execute(
            "SELECT b.epoch, r.kind, b.preserved FROM boundary b JOIN record r ON r.id = b.summary_id").fetchone()
        self.assertEqual((epoch, summary_kind, preserved), (0, "compact_summary", '["h1"]'))
        self.assertEqual(self.db.execute("SELECT count(*), max(output) FROM request").fetchone(), (1, 50))

    def test_redaction_and_search(self):
        st = archive.ingest(self.db, corpus.freeze(self.root))
        self.assertEqual(st.redacted, Counter({"anthropic": 1}))
        hits = archive.search(self.db, archive.quote("курсор"))
        self.assertEqual({h.kind for h in hits}, {Kind.HUMAN, Kind.COMPACT_SUMMARY})
        self.assertNotIn("sk-ant", self.db.execute("SELECT text FROM record WHERE kind = 'human'").fetchone()[0])
        only_human = archive.search(self.db, archive.quote("курсор"), kinds=frozenset({Kind.HUMAN}))
        self.assertEqual([h.author for h in only_human], ["user"])
        tri = archive.search(self.db, archive.quote("рсор в mem"), archive.Index.TRIGRAM)
        self.assertEqual(len(tri), 1)

    def test_incremental_append_and_rewrite_refused(self):
        archive.ingest(self.db, corpus.freeze(self.root))
        fx.append(self.path, [fx.human("третий промпт")])
        st = archive.ingest(self.db, corpus.freeze(self.root))
        self.assertEqual(st.lines, 1)
        self.assertEqual(self.db.execute("SELECT max(epoch) FROM record").fetchone()[0], 1)
        self.assertEqual(self.db.execute("SELECT count(*) FROM record WHERE kind = 'human'").fetchone()[0], 3)
        self.assertEqual(archive.ingest(self.db, corpus.freeze(self.root)).files, 0)
        fx.write(self.path, [fx.human("переписан"), fx.human("целиком"), fx.human("файл"), fx.human("x"),
                             fx.human("y"), fx.human("z"), fx.human("w")])
        with self.assertRaises(ValueError):
            archive.ingest(self.db, corpus.freeze(self.root))



class Space(unittest.TestCase):
    def test_check_space_on_missing_dir(self):
        with tempfile.TemporaryDirectory() as t:
            target = Path(t) / "a/b/archive.db"
            archive.check_space(target, min_free=0)
            with self.assertRaises(SystemExit):
                archive.check_space(target, min_free=1 << 62)


if __name__ == "__main__":
    unittest.main()
