from __future__ import annotations

import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_content.cli import (
    _sync_operator_events_to_vps,
    _write_topic_inbox,
)
from agent_content.integrations.codex_story_exporter import StoryDocument
from agent_content.integrations.codex_topic_exporter import TopicDocument, TopicSourceMessage
from agent_content.integrations.nazai_inbox import NazAiInbox
from agent_content.integrations.operator_event_exporter import (
    OperatorEventDocument,
    OperatorEventExporter,
)


DATE = "2026-07-21"
PROJECT = "Naz_AI_Bot_clean"


def _normalize_identity_text(value: str) -> str:
    return " ".join(value.split())


def _topic_id(session_id: str, message: TopicSourceMessage) -> str:
    seed = "\0".join(
        (
            session_id,
            message.timestamp,
            message.role.casefold(),
            _normalize_identity_text(message.text),
        )
    )
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()[:12]


def _pair(
    *,
    session_id: str = "raw-session-uuid-550e8400-e29b-41d4-a716-446655440000",
    title: str = "Repair the queue without publishing",
    messages: tuple[TopicSourceMessage, ...] | None = None,
    project: str = PROJECT,
    boundary_reason: str = "task_request",
    closed: bool = True,
    publishable: bool = True,
    topic_body: str | None = None,
) -> tuple[TopicDocument, StoryDocument]:
    if messages is None:
        messages = (
            TopicSourceMessage(
                timestamp="2026-07-21T08:00:00+03:00",
                role="user",
                text="Repair the queue without publishing.",
            ),
            TopicSourceMessage(
                timestamp="2026-07-21T08:01:00+03:00",
                role="codex",
                text="The investigation is still in progress.",
            ),
        )
    identity = messages[0]
    topic_id = _topic_id(session_id, identity)
    topic_text = topic_body or (
        f"# {title}\n\nProject: {project}\nDate: {DATE}\n"
        f"Topic: {topic_id}\nChat: {session_id}\n\n"
        + "\n\n".join(item.text for item in messages)
        + "\n"
    )
    relative_path = Path(project) / DATE / f"{DATE}-0800--event--t-{topic_id}.md"
    topic = TopicDocument(
        relative_path=relative_path,
        text=topic_text,
        project_name=project,
        date=DATE,
        session_id=session_id,
        topic_id=topic_id,
        title=title,
        publishable=publishable,
        closed=closed,
        boundary_reason=boundary_reason,
        source_messages=messages,
    )
    story = StoryDocument(
        relative_path=relative_path,
        text=f"# {title}\n\nGrounded editorial story bytes.\n",
        project_name=project,
        date=DATE,
        session_id=session_id,
        topic_id=topic_id,
        title=title,
        publishable=publishable,
        source_hash=hashlib.sha256(topic_text.encode("utf-8")).hexdigest(),
    )
    return topic, story


def _proof_messages() -> tuple[TopicSourceMessage, ...]:
    return (
        TopicSourceMessage(
            timestamp="2026-07-21T08:00:00+03:00",
            role="user",
            text="Repair the queue without publishing.",
        ),
        TopicSourceMessage(
            timestamp="2026-07-21T08:01:00+03:00",
            role="codex",
            text=(
                "Actual cause: an expired local lock\n"
                "Evidence: the deterministic fixture reproduced the lock\n"
                "Technical result: the local tests passed"
            ),
        ),
    )


class OperatorEventExporterTests(unittest.TestCase):
    def test_repeated_export_has_stable_event_id_refs_and_bytes(self) -> None:
        topic, story = _pair(messages=_proof_messages())
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "content_inbox" / "operator_events"
            exporter = OperatorEventExporter(root)
            first_documents = exporter.build_documents([topic], [story])
            first = first_documents[0].payload
            first_result = exporter.write_documents(first_documents)
            first_bytes = Path(first_result["documents"][0]).read_bytes()

            second_documents = exporter.build_documents([topic], [story])
            second = second_documents[0].payload
            second_result = exporter.write_documents(second_documents)
            second_bytes = Path(second_result["documents"][0]).read_bytes()

        first_event = first["events"][0]
        second_event = second["events"][0]
        self.assertEqual(first_event["event_id"], second_event["event_id"])
        self.assertEqual(first_event["source_session_refs"], second_event["source_session_refs"])
        self.assertEqual(first_event["source_message_refs"], second_event["source_message_refs"])
        self.assertEqual(first_bytes, second_bytes)

        seed = "\0".join(
            (
                "operator-event-set.v1",
                PROJECT,
                DATE,
                topic.topic_id,
                story.source_hash,
            )
        )
        expected = "oev-" + hashlib.sha256(seed.encode("utf-8")).hexdigest()[:24]
        self.assertEqual(expected, first_event["event_id"])

    def test_independent_topics_produce_separate_events(self) -> None:
        first = _pair(session_id="session-one", messages=_proof_messages())
        second_messages = (
            TopicSourceMessage(
                timestamp="2026-07-21T09:00:00+03:00",
                role="user",
                text="Validate a different local worker.",
            ),
            TopicSourceMessage(
                timestamp="2026-07-21T09:01:00+03:00",
                role="codex",
                text=(
                    "Actual cause: a stale fixture\n"
                    "Evidence: a local assertion exposed it\n"
                    "Technical result: the replacement fixture passed"
                ),
            ),
        )
        second = _pair(
            session_id="session-two",
            title="Validate a different local worker",
            messages=second_messages,
        )
        with tempfile.TemporaryDirectory() as tmp:
            exporter = OperatorEventExporter(Path(tmp) / "operator_events")
            documents = exporter.build_documents(
                [first[0], second[0]],
                [first[1], second[1]],
            )

        self.assertEqual(2, len(documents))
        self.assertEqual(2, len({item.relative_path for item in documents}))
        self.assertEqual(
            2,
            len({item.payload["events"][0]["event_id"] for item in documents}),
        )

    def test_unconfirmed_cause_evidence_and_result_fail_closed(self) -> None:
        topic, story = _pair()
        with tempfile.TemporaryDirectory() as tmp:
            event = OperatorEventExporter(Path(tmp) / "events").build_documents(
                [topic], [story]
            )[0].payload["events"][0]

        facts = event["event_facts"]
        self.assertIsNone(facts["actual_cause"]["value"])
        self.assertEqual([], facts["actual_cause"]["source_message_refs"])
        self.assertEqual([], facts["evidence"])
        self.assertIsNone(facts["technical_result"]["value"])
        self.assertEqual("needs_review", event["content_status"])
        self.assertIn("actual_cause_unconfirmed", event["reason_codes"])
        self.assertIn("evidence_unconfirmed", event["reason_codes"])
        self.assertIn("technical_result_unconfirmed", event["reason_codes"])

    def test_tool_roles_cannot_supply_cause_evidence_or_result(self) -> None:
        messages = (
            TopicSourceMessage(
                timestamp="2026-07-21T08:00:00+03:00",
                role="user",
                text="Repair the queue without publishing.",
            ),
            TopicSourceMessage(
                timestamp="2026-07-21T08:01:00+03:00",
                role="codex",
                text="The visible investigation remains in progress.",
            ),
            TopicSourceMessage(
                timestamp="2026-07-21T08:02:00+03:00",
                role="tool",
                text="Actual cause: tool-only cause\nEvidence: tool-only evidence",
            ),
            TopicSourceMessage(
                timestamp="2026-07-21T08:03:00+03:00",
                role="tool_result",
                text="Technical result: tool-only result",
            ),
        )
        topic, story = _pair(messages=messages)
        with tempfile.TemporaryDirectory() as tmp:
            event = OperatorEventExporter(Path(tmp) / "events").build_documents(
                [topic], [story]
            )[0].payload["events"][0]

        facts = event["event_facts"]
        self.assertIsNone(facts["actual_cause"]["value"])
        self.assertEqual([], facts["evidence"])
        self.assertIsNone(facts["technical_result"]["value"])
        self.assertEqual(2, len(event["source_message_refs"]))
        self.assertEqual("needs_review", event["content_status"])

    def test_explicit_visible_proof_is_individually_grounded(self) -> None:
        topic, story = _pair(messages=_proof_messages())
        with tempfile.TemporaryDirectory() as tmp:
            event = OperatorEventExporter(Path(tmp) / "events").build_documents(
                [topic], [story]
            )[0].payload["events"][0]

        facts = event["event_facts"]
        self.assertEqual("an expired local lock", facts["actual_cause"]["value"])
        self.assertEqual(1, len(facts["actual_cause"]["source_message_refs"]))
        self.assertEqual(1, len(facts["evidence"]))
        self.assertEqual(1, len(facts["evidence"][0]["source_message_refs"]))
        self.assertEqual("the local tests passed", facts["technical_result"]["value"])
        self.assertEqual("ready", event["content_status"])

    def test_metadata_title_without_visible_source_is_not_a_grounded_summary(self) -> None:
        topic, story = _pair(
            title="Session index metadata title absent from every visible message",
            messages=_proof_messages(),
        )
        with tempfile.TemporaryDirectory() as tmp:
            event = OperatorEventExporter(Path(tmp) / "events").build_documents(
                [topic], [story]
            )[0].payload["events"][0]

        summary = event["event_facts"]["event_summary"]
        self.assertIsNone(summary["value"])
        self.assertEqual([], summary["source_message_refs"])
        self.assertEqual("needs_review", event["content_status"])
        self.assertIn("event_summary_unconfirmed", event["reason_codes"])

    def test_evidence_and_open_questions_are_bounded_for_consumer_schema(self) -> None:
        labelled_lines = ["Actual cause: bounded fixture"]
        labelled_lines.extend(f"Evidence: proof item {index}" for index in range(9))
        labelled_lines.append("Technical result: bounded fixture passed")
        labelled_lines.extend(f"Open question: question {index}" for index in range(9))
        messages = (
            TopicSourceMessage(
                timestamp="2026-07-21T08:00:00+03:00",
                role="user",
                text="Repair the queue without publishing.",
            ),
            TopicSourceMessage(
                timestamp="2026-07-21T08:01:00+03:00",
                role="codex",
                text="\n".join(labelled_lines),
            ),
        )
        topic, story = _pair(messages=messages)
        with tempfile.TemporaryDirectory() as tmp:
            event = OperatorEventExporter(Path(tmp) / "events").build_documents(
                [topic], [story]
            )[0].payload["events"][0]

        self.assertEqual(8, len(event["event_facts"]["evidence"]))
        self.assertEqual(8, len(event["operator_commentary"]["open_questions"]))
        self.assertIn("evidence_truncated", event["reason_codes"])
        self.assertIn("open_questions_truncated", event["reason_codes"])
        self.assertEqual("needs_review", event["content_status"])

    def test_safety_chunk_boundary_is_ambiguous_and_fail_closed(self) -> None:
        topic, story = _pair(messages=_proof_messages(), boundary_reason="safety_chunk")
        with tempfile.TemporaryDirectory() as tmp:
            event = OperatorEventExporter(Path(tmp) / "events").build_documents(
                [topic], [story]
            )[0].payload["events"][0]

        self.assertEqual("needs_review", event["content_status"])
        self.assertIn("ambiguous_event_boundary", event["reason_codes"])

    def test_raw_session_chat_and_private_values_are_not_serialized(self) -> None:
        raw_session = "550e8400-e29b-41d4-a716-446655440000"
        raw_chat = (
            "This is a full private chat canary that must never be copied into the sidecar. "
            f"The embedded session is {raw_session}. "
            "token=sk-ABCDEFGHIJKLMNOPQRSTUVWXYZ012345 and user@example.com at "
            r"C:\Users\Private\secret.txt"
        )
        messages = (
            TopicSourceMessage(
                timestamp="2026-07-21T08:00:00+03:00",
                role="user",
                text=raw_chat,
            ),
            TopicSourceMessage(
                timestamp="2026-07-21T08:01:00+03:00",
                role="codex",
                text="Evidence: token=sk-ZYXWVUTSRQPONMLKJIHGFEDCBA987654",
            ),
        )
        topic, story = _pair(
            session_id=raw_session,
            title="Private source requires review",
            messages=messages,
        )
        with tempfile.TemporaryDirectory() as tmp:
            exporter = OperatorEventExporter(Path(tmp) / "operator_events")
            result = exporter.export([topic], [story])
            raw_json = Path(result["documents"][0]).read_text(encoding="utf-8")
            payload = json.loads(raw_json)

        self.assertNotIn(raw_session, raw_json)
        self.assertNotIn(raw_chat, raw_json)
        self.assertNotIn("user@example.com", raw_json)
        self.assertNotIn("sk-ABCDEFGHIJKLMNOPQRSTUVWXYZ012345", raw_json)
        self.assertNotIn("C:\\Users\\Private", raw_json)
        self.assertNotIn(story.session_id, raw_json)
        event = payload["events"][0]
        self.assertTrue(all(ref.startswith("session-") for ref in event["source_session_refs"]))
        self.assertTrue(all(ref.startswith("message-") for ref in event["source_message_refs"]))
        self.assertEqual("needs_review", event["privacy_status"])
        self.assertEqual("needs_review", event["content_status"])

    def test_publication_copy_is_separate_and_null(self) -> None:
        topic, story = _pair(messages=_proof_messages())
        with tempfile.TemporaryDirectory() as tmp:
            event = OperatorEventExporter(Path(tmp) / "events").build_documents(
                [topic], [story]
            )[0].payload["events"][0]

        self.assertIsNone(event["publication_copy_ref"])
        self.assertNotIn("publication_copy", event["event_facts"])

    def test_source_change_changes_source_hash_and_event_id(self) -> None:
        first = _pair(messages=_proof_messages(), topic_body="first source snapshot")
        second = _pair(messages=_proof_messages(), topic_body="second source snapshot")
        with tempfile.TemporaryDirectory() as tmp:
            exporter = OperatorEventExporter(Path(tmp) / "events")
            first_payload = exporter.build_documents([first[0]], [first[1]])[0].payload
            second_payload = exporter.build_documents([second[0]], [second[1]])[0].payload

        self.assertNotEqual(first_payload["source_hash"], second_payload["source_hash"])
        self.assertNotEqual(
            first_payload["events"][0]["event_id"],
            second_payload["events"][0]["event_id"],
        )

    def test_story_source_hash_mismatch_is_rejected(self) -> None:
        topic, story = _pair()
        bad_story = StoryDocument(
            relative_path=story.relative_path,
            text=story.text,
            project_name=story.project_name,
            date=story.date,
            session_id=story.session_id,
            topic_id=story.topic_id,
            title=story.title,
            publishable=story.publishable,
            source_hash="0" * 64,
        )
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(RuntimeError, "source hash mismatch"):
                OperatorEventExporter(Path(tmp) / "events").build_documents(
                    [topic], [bad_story]
                )

    def test_story_project_path_mismatch_is_rejected(self) -> None:
        topic, story = _pair()
        bad_story = StoryDocument(
            relative_path=Path("OtherProject") / DATE / story.relative_path.name,
            text=story.text,
            project_name=story.project_name,
            date=story.date,
            session_id=story.session_id,
            topic_id=story.topic_id,
            title=story.title,
            publishable=story.publishable,
            source_hash=story.source_hash,
        )
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(RuntimeError, "Story/topic path mismatch"):
                OperatorEventExporter(Path(tmp) / "events").build_documents(
                    [topic], [bad_story]
                )

    def test_atomic_rewrite_restores_previous_tree_without_partial_files(self) -> None:
        first = _pair(messages=_proof_messages())
        second_messages = (
            TopicSourceMessage(
                timestamp="2026-07-21T09:00:00+03:00",
                role="user",
                text="Repair another queue.",
            ),
            TopicSourceMessage(
                timestamp="2026-07-21T09:01:00+03:00",
                role="codex",
                text="Actual cause: lock\nEvidence: fixture\nTechnical result: passed",
            ),
        )
        second = _pair(
            session_id="second-session",
            title="Repair another queue",
            messages=second_messages,
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "content_inbox" / "operator_events"
            exporter = OperatorEventExporter(root)
            original_result = exporter.export([first[0]], [first[1]])
            original_path = Path(original_result["documents"][0])
            original_bytes = original_path.read_bytes()
            replacement = exporter.build_documents(
                [first[0], second[0]],
                [first[1], second[1]],
            )
            real_write_bytes = Path.write_bytes
            call_count = 0

            def fail_second_write(path: Path, data: bytes) -> int:
                nonlocal call_count
                call_count += 1
                if call_count == 2:
                    raise OSError("synthetic interrupted write")
                return real_write_bytes(path, data)

            with patch.object(Path, "write_bytes", new=fail_second_write):
                with self.assertRaisesRegex(OSError, "synthetic interrupted write"):
                    exporter.write_documents(replacement)

            self.assertEqual(original_bytes, original_path.read_bytes())
            self.assertEqual([original_path], list(root.rglob("*.json")))
            leftovers = [
                path
                for path in root.parent.iterdir()
                if path.name.startswith(".operator_events.")
            ]
            self.assertEqual([], leftovers)

    def test_cli_writes_separate_sidecar_without_changing_markdown_bytes(self) -> None:
        topic, story = _pair(messages=_proof_messages())
        with tempfile.TemporaryDirectory() as tmp:
            baseline_result = NazAiInbox(
                local_path=str(Path(tmp) / "baseline"),
                inbox_dir="content_inbox/agent_content",
            ).write_documents({story.relative_path: story.text})
            baseline_bytes = (
                Path(baseline_result["inbox_dir"]) / story.relative_path
            ).read_bytes()
            local_root = Path(tmp) / "naz"
            environment = {
                "NAZAI_LOCAL_PATH": str(local_root),
                "NAZAI_INBOX_DIR": "content_inbox/agent_content",
            }
            with (
                patch.dict(os.environ, environment, clear=False),
                patch("agent_content.cli.CodexTopicExporter") as topic_exporter,
                patch("agent_content.cli.CodexStoryExporter") as story_exporter,
            ):
                topic_exporter.return_value.build_documents.return_value = [topic]
                story_exporter.return_value.build_documents.return_value = [story]
                result, returned_stories = _write_topic_inbox([object()])

            markdown_path = Path(result["inbox_dir"]) / story.relative_path
            event_root = Path(result["operator_events_dir"])
            event_path = event_root / PROJECT / DATE / f"t-{topic.topic_id}.json"
            self.assertEqual(baseline_bytes, markdown_path.read_bytes())
            self.assertEqual([story], returned_stories)
            self.assertEqual("ready", result["operator_events_status"])
            self.assertTrue(event_path.is_file())
            self.assertEqual(Path(result["inbox_dir"]).parent, event_root.parent)
            self.assertNotEqual(Path(result["inbox_dir"]), event_root)
            self.assertEqual([], list(Path(result["inbox_dir"]).rglob("*.json")))

    def test_operator_event_failure_does_not_block_markdown(self) -> None:
        topic, story = _pair()
        with tempfile.TemporaryDirectory() as tmp:
            baseline_result = NazAiInbox(
                local_path=str(Path(tmp) / "baseline"),
                inbox_dir="content_inbox/agent_content",
            ).write_documents({story.relative_path: story.text})
            baseline_bytes = (
                Path(baseline_result["inbox_dir"]) / story.relative_path
            ).read_bytes()
            environment = {
                "NAZAI_LOCAL_PATH": str(Path(tmp) / "naz"),
                "NAZAI_INBOX_DIR": "content_inbox/agent_content",
            }
            with (
                patch.dict(os.environ, environment, clear=False),
                patch("agent_content.cli.CodexTopicExporter") as topic_exporter,
                patch("agent_content.cli.CodexStoryExporter") as story_exporter,
                patch.object(
                    OperatorEventExporter,
                    "build_documents",
                    side_effect=RuntimeError("synthetic shadow failure"),
                ),
            ):
                topic_exporter.return_value.build_documents.return_value = [topic]
                story_exporter.return_value.build_documents.return_value = [story]
                result, _ = _write_topic_inbox([object()])

            markdown_path = Path(result["inbox_dir"]) / story.relative_path
            self.assertEqual(baseline_bytes, markdown_path.read_bytes())
            self.assertEqual("failed", result["operator_events_status"])
            self.assertEqual(
                "operator_event_export_failed",
                result["operator_events_reason_code"],
            )

    def test_vps_sync_validates_and_uses_separate_json_root(self) -> None:
        topic, story = _pair(messages=_proof_messages())
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "content_inbox" / "operator_events"
            OperatorEventExporter(root).export([topic], [story])
            with patch("agent_content.cli._run_checked") as run_checked:
                _sync_operator_events_to_vps(root, "deploy@example", "/opt/naz")

        self.assertEqual(3, run_checked.call_count)
        commands = [call.args[0] for call in run_checked.call_args_list]
        self.assertTrue(any(".operator_events.staging" in " ".join(cmd) for cmd in commands))
        self.assertTrue(any("/content_inbox/operator_events" in " ".join(cmd) for cmd in commands))
        self.assertFalse(any("/content_inbox/agent_content" in " ".join(cmd) for cmd in commands))

    def test_vps_sync_rejects_invalid_json_before_network_helper(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "operator_events"
            target = root / PROJECT / DATE / "t-0123456789ab.json"
            target.parent.mkdir(parents=True)
            target.write_text("not-json", encoding="utf-8")
            with patch("agent_content.cli._run_checked") as run_checked:
                with self.assertRaisesRegex(RuntimeError, "invalid OperatorEvent JSON"):
                    _sync_operator_events_to_vps(root, "deploy@example", "/opt/naz")
            run_checked.assert_not_called()

    def test_writer_rejects_unsafe_paths_before_replacing_existing_tree(self) -> None:
        topic, story = _pair(messages=_proof_messages())
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "operator_events"
            exporter = OperatorEventExporter(root)
            original = exporter.export([topic], [story])
            original_path = Path(original["documents"][0])
            original_bytes = original_path.read_bytes()
            unsafe = OperatorEventDocument(
                relative_path=Path("..") / DATE / f"t-{topic.topic_id}.json",
                payload={},
            )
            with self.assertRaisesRegex(ValueError, "Unsafe OperatorEvent"):
                exporter.write_documents([unsafe])
            self.assertEqual(original_bytes, original_path.read_bytes())


if __name__ == "__main__":
    unittest.main()
