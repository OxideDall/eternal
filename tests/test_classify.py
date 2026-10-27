import unittest

from eternal.classify import RESULT_HEAD, RESULT_TAIL, classify
from eternal.kinds import Boundary, Kind, Record, Request, Snapshot, Trigger, Via

from . import fixtures as fx


def kinds(e: dict) -> list[Kind]:
    return [ev.kind for ev in classify(e, 0) if isinstance(ev, Record)]


class PromptProvenance(unittest.TestCase):
    def test_human_typed(self):
        self.assertEqual(kinds(fx.human("почини курсор")), [Kind.HUMAN])

    def test_reminder_stripped_from_human(self):
        (r,) = [x for x in classify(fx.human("сделай <system-reminder>секрет</system-reminder> тест"), 0)]
        self.assertEqual(r.text, "сделай  тест")

    def test_reminder_only_is_meta(self):
        self.assertEqual(kinds(fx.human("<system-reminder>x</system-reminder>")), [Kind.META])

    def test_task_notification_by_origin_and_tag(self):
        self.assertEqual(kinds(fx.user_raw("done", origin={"kind": "task-notification"})), [Kind.TASK_NOTIFICATION])
        self.assertEqual(kinds(fx.user_raw("<task-notification>x</task-notification>")), [Kind.TASK_NOTIFICATION])

    def test_peer_and_scheduled_meta(self):
        self.assertEqual(kinds(fx.user_raw("hi", isMeta=True, origin={"kind": "peer"})), [Kind.PEER])
        self.assertEqual(kinds(fx.user_raw("cron", isMeta=True, promptSource="system")), [Kind.SCHEDULED])
        self.assertEqual(kinds(fx.user_raw("caveat", isMeta=True)), [Kind.META])

    def test_legacy_without_origin(self):
        self.assertEqual(kinds(fx.user_raw("старый промпт")), [Kind.HUMAN])
        self.assertEqual(kinds(fx.user_raw("<command-name>/compact</command-name>")), [Kind.COMMAND])
        self.assertEqual(kinds(fx.user_raw("<local-command-stdout>ok</local-command-stdout>")), [Kind.COMMAND_OUTPUT])
        self.assertEqual(kinds(fx.user_raw("<unknown-tag>x")), [Kind.META])

    def test_interrupt_is_not_a_prompt(self):
        self.assertEqual(kinds(fx.user_raw([{"type": "text", "text": "[Request interrupted by user]"}])), [Kind.INTERRUPT])

    def test_queued_command_attachments(self):
        self.assertEqual(kinds(fx.queued("добавь тест")), [Kind.HUMAN])
        self.assertEqual(kinds(fx.queued("добавь тест", origin="human")), [Kind.HUMAN])
        self.assertEqual(kinds(fx.queued("msg", origin="peer", meta=True)), [Kind.PEER])
        self.assertEqual(kinds(fx.queued("<task-notification>", mode="task-notification")), [Kind.TASK_NOTIFICATION])
        self.assertEqual(kinds(fx.queued([{"type": "text", "text": "список"}])), [Kind.HUMAN])


class DeliveryPath(unittest.TestCase):
    def via(self, e: dict) -> Via | None:
        (r,) = classify(e, 0)
        return r.via

    def test_via_by_prompt_source(self):
        self.assertEqual(self.via(fx.human("x")), Via.TYPED)
        self.assertEqual(self.via(fx.human("x", promptSource="queued")), Via.QUEUED)
        self.assertEqual(self.via(fx.user_raw("x", promptSource="sdk")), Via.SDK)
        self.assertEqual(self.via(fx.user_raw("x")), Via.LEGACY)
        self.assertEqual(self.via(fx.queued("x")), Via.ATTACHMENT)

    def test_via_only_on_human(self):
        self.assertIsNone(self.via(fx.user_raw("<task-notification>x", origin={"kind": "task-notification"})))
        self.assertIsNone(self.via(fx.queued("x", origin="peer")))


class AssistantAndTools(unittest.TestCase):
    def test_text_and_usage(self):
        evs = list(classify(fx.text("ответ", "m1", {"input_tokens": 2, "cache_creation_input_tokens": 3,
                                                     "cache_read_input_tokens": 5, "output_tokens": 7}), 4))
        self.assertEqual([type(e) for e in evs], [Record, Request])
        self.assertEqual(evs[1].context, 10)

    def test_synthetic_error_is_meta_without_request(self):
        evs = list(classify(fx.text("API Error", "m2", model="<synthetic>"), 0))
        self.assertEqual([e.kind for e in evs if isinstance(e, Record)], [Kind.META])
        self.assertFalse(any(isinstance(e, Request) for e in evs))

    def test_tool_use_args_and_render(self):
        (r, _) = classify(fx.tool_use("Edit", {"file_path": "/a/b.py", "old_string": "x" * 999}, "m3", "t1"), 0)
        self.assertEqual((r.kind, r.tool, r.args), (Kind.TOOL_USE, "Edit", {"file_path": "/a/b.py"}))
        self.assertEqual(r.text, "Edit: file_path=/a/b.py")

    def test_tool_result_task_id_error_and_cap(self):
        (r,) = classify(fx.tool_result("t2", "Task #3 created", {"task": {"id": "3", "subject": "s"}}), 0)
        self.assertEqual(r.args, {"task_id": "3"})
        (r,) = classify(fx.tool_result("t3", "boom", error=True), 0)
        self.assertEqual(r.text, "error: boom")
        (r,) = classify(fx.tool_result("t4", "a" * 10_000), 0)
        self.assertEqual((r.chars, len(r.text)), (10_000, RESULT_HEAD + RESULT_TAIL + 3))

    def test_long_human_kept_whole(self):
        (r,) = classify(fx.human("я" * 50_000), 0)
        self.assertEqual(len(r.text), 50_000)


class SystemEntries(unittest.TestCase):
    def test_boundary_preserved_union(self):
        e = fx.boundary(("a", "b"))
        e["compactMetadata"]["preservedMessages"]["allUuids"] = ["b", "c"]
        (b,) = classify(e, 9)
        self.assertIsInstance(b, Boundary)
        self.assertEqual((b.preserved, b.trigger, b.line), (("a", "b", "c"), Trigger.AUTO, 9))

    def test_boundary_unknown_trigger(self):
        (b,) = classify(fx.boundary(trigger="weird"), 0)
        self.assertEqual(b.trigger, Trigger.UNKNOWN)

    def test_summaries(self):
        self.assertEqual(kinds(fx.summary("Summary: ...")), [Kind.COMPACT_SUMMARY])
        away = {"type": "system", "subtype": "away_summary", "content": "recap", "uuid": "z"}
        self.assertEqual(kinds(away), [Kind.AWAY_SUMMARY])

    def test_hook_context_is_not_a_record(self):
        a = {"type": "hook_additional_context", "content": ["[eternal registry] Session x"], "hookName": "SessionStart",
             "hookEvent": "SessionStart", "toolUseID": "SessionStart"}
        self.assertEqual(list(classify({"type": "attachment", "attachment": a}, 0)), [])

    def test_prompt_snapshot_sizes(self):
        e = {"type": "attachment", "attachment": {"type": "prompt_snapshot", "systemPrompt": ["abc", "de"],
                                                  "tools": [{"name": "Bash", "description": "d", "schema": {}}]}}
        (s,) = classify(e, 0)
        self.assertIsInstance(s, Snapshot)
        self.assertEqual((s.system_chars, s.tools), (5, 1))
        self.assertGreater(s.tools_chars, 0)


if __name__ == "__main__":
    unittest.main()
