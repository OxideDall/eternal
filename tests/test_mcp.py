"""MCP server: JSON-RPC handling, search and zoom over a pinned archive, stdio framing."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from eternal import archive, corpus, live, mcp

from . import fixtures as fx

SID = "abcd1234-0000-0000-0000-000000000001"
REPO = Path(__file__).resolve().parents[1]
LONG = "/home/u/projects/some/really/long/directory/structure/that/goes/deep/very_long_module_name.py"


def at(e: dict, day: int) -> dict:
    return {**e, "timestamp": fx.ts(day)}


def rpc(method: str, params: dict | None = None, rid: int | None = 1) -> dict:
    m = {"jsonrpc": "2.0", "method": method, "params": params or {}}
    return m if rid is None else {**m, "id": rid}


class Server(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        base = Path(cls.tmp.name)
        root = base / "projects"
        fx.write(root / f"-home-u-proj/{SID}.jsonl", [
            at(fx.human("почини курсор в memory.ts"), 1), at(fx.text("курсор починен", "m1"), 1),
            at(fx.human("теперь кодовое слово малахит"), 3), at(fx.text("принято: малахитовый", "m2"), 3),
            at(fx.tool_use("Write", {"file_path": LONG, "content": "x"}, "m3", "t1"), 3),
            at(fx.tool_result("t1", "ok"), 3)])
        cls.db_path = base / "archive.db"
        db = archive.connect(cls.db_path)
        archive.ingest(db, corpus.freeze(root))
        db.close()

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def server(self, **env) -> mcp.Server:
        return mcp.Server({"ETERNAL_ARCHIVE": str(self.db_path), **env})

    def call(self, s: mcp.Server, tool: str, **args) -> tuple[str, bool]:
        r = s.handle(rpc("tools/call", {"name": tool, "arguments": args}))["result"]
        return r["content"][0]["text"], r["isError"]

    def test_initialize_list_and_unknowns(self):
        s = self.server()
        init = s.handle(rpc("initialize", {"protocolVersion": "2025-03-26"}))["result"]
        self.assertEqual((init["protocolVersion"], init["serverInfo"]["name"]), ("2025-03-26", "eternal"))
        self.assertIsNone(s.handle(rpc("notifications/initialized", rid=None)))
        self.assertEqual([t["name"] for t in s.handle(rpc("tools/list"))["result"]["tools"]], ["search", "zoom"])
        self.assertEqual(s.handle(rpc("ping"))["result"], {})
        self.assertEqual(s.handle(rpc("resources/list"))["error"]["code"], mcp.Code.METHOD)
        self.assertEqual(s.handle(rpc("tools/call", {"name": "nope"}))["error"]["code"], mcp.Code.PARAMS)

    def test_search_matches_inflections_with_addresses(self):
        text, bad = self.call(self.server(), "search", query="малахит")
        self.assertFalse(bad)
        self.assertIn(f"{SID[:8]}:L2 2026-10-03 00:00 user/human [proj]", text)
        self.assertIn(f"{SID[:8]}:L3 ", text)

    def test_exact_and_filters(self):
        s = self.server()
        self.assertIn(":L0 ", self.call(s, "search", query="memory.ts", exact=True)[0])
        self.assertNotIn(":L3 ", self.call(s, "search", query="малахит", author="user")[0])
        self.assertIn("no hits", self.call(s, "search", query="малахит", session="ffff")[0])
        self.assertIn("no hits", self.call(s, "search", query="курсор", since="2026-10-02")[0])

    def test_zoom_marks_focus_between_neighbours(self):
        text, bad = self.call(self.server(), "zoom", address=f"{SID[:8]}:L2", before=1, after=1)
        self.assertFalse(bad)
        lines = text.splitlines()
        self.assertEqual(lines[0], f"session {SID} [proj]")
        self.assertEqual([x[:4] for x in lines[1:]], ["  L1", "> L2", "  L3"])
        self.assertIn("малахит", lines[2])

    def test_brief_zoom_is_a_timeline_with_short_paths(self):
        s = self.server()
        text, bad = self.call(s, "zoom", address=f"{SID[:8]}:L0", after=30, brief=True)
        self.assertFalse(bad)
        self.assertEqual(len(text.splitlines()), 2 + 5)
        self.assertIn("file_path=…/deep/very_long_module_name.py", text)
        acts = self.call(s, "zoom", address=f"{SID[:8]}:L0", after=10, kind="tool_use")[0]
        self.assertEqual([x.split()[0] for x in acts.splitlines()[2:]], ["L4"])
        self.assertIn(LONG, acts)
        self.assertTrue(self.call(s, "zoom", address=f"{SID[:8]}:L0", kind="bogus")[1])

    def test_before_hides_later_records(self):
        s = self.server(ETERNAL_BEFORE="2026-10-02")
        self.assertIn("no hits", self.call(s, "search", query="малахит")[0])
        text, bad = self.call(s, "zoom", address=f"{SID[:8]}:L2")
        self.assertTrue(bad)
        self.assertNotIn("малахит", self.call(s, "zoom", address=f"{SID[:8]}:L0", after=5)[0])

    def test_exclude_hides_sessions(self):
        s = self.server(ETERNAL_EXCLUDE=f" {SID[:4].upper()} ,bogus%")
        self.assertEqual(s.exclude, (SID[:4],))
        self.assertIn("no hits", self.call(s, "search", query="малахит")[0])
        self.assertTrue(self.call(s, "zoom", address=f"{SID[:8]}:L2")[1])

    def test_live_exclusions_apply_unless_pinned(self):
        """Sessions in the live dir's exclusions vanish from live search, not from a pinned archive."""
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, {"ETERNAL_LIVE": d}), \
                mock.patch.object(mcp.live, "refresh"):
            shutil.copy(self.db_path, live.db_path())
            s = mcp.Server({})
            self.assertFalse(self.call(s, "search", query="малахит")[0].startswith("no hits"))
            live.exclude([SID])
            self.assertTrue(self.call(s, "search", query="малахит")[0].startswith("no hits"))
            self.assertTrue(self.call(s, "zoom", address=f"{SID[:8]}:L2")[1])
            self.assertFalse(self.call(self.server(), "search", query="малахит")[0].startswith("no hits"))
            s.db.close()

    def test_bad_arguments_are_tool_errors(self):
        s = self.server()
        for tool, args in (("zoom", {"address": "nonsense"}), ("zoom", {"address": "ffff:L1"}),
                           ("search", {"query": " "}), ("search", {"query": "x", "author": "bogus"}),
                           ("search", {"query": "курсор", "session": "a%"}), ("search", {"query": "ab", "exact": True})):
            self.assertTrue(self.call(s, tool, **args)[1], (tool, args))

    def test_stdio_roundtrip(self):
        lines = [rpc("initialize", rid=1), rpc("notifications/initialized", rid=None), rpc("tools/list", rid=2)]
        stdin = "".join(json.dumps(m) + "\n" for m in lines) + "not json\n"
        env = {**os.environ, "ETERNAL_ARCHIVE": str(self.db_path)}
        p = subprocess.run([sys.executable, "-m", "eternal.mcp"], input=stdin, capture_output=True, text=True,
                           env=env, cwd=REPO, timeout=30)
        replies = [json.loads(x) for x in p.stdout.splitlines()]
        self.assertEqual([r.get("id") for r in replies], [1, 2, None])
        self.assertEqual(replies[2]["error"]["code"], mcp.Code.PARSE)


if __name__ == "__main__":
    unittest.main()
