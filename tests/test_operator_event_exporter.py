from __future__ import annotations

import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from contextlib import nullcontext, redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import agent_content.cli as cli
from agent_content.cli import (
    VpsSyncError,
    _remote_release_paths,
    _remote_release_transaction_script,
    _sync_nazai_release_to_vps,
    _sync_vps_if_requested,
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


def _write_release_fixture(
    root: Path, *, operator_status: str = "ready",
) -> tuple[dict[str, object], TopicDocument, StoryDocument]:
    topic, story = _pair(messages=_proof_messages())
    local_root = root / "naz"
    environment = {
        "NAZAI_LOCAL_PATH": str(local_root),
        "NAZAI_INBOX_DIR": "content_inbox/agent_content",
    }
    event_patch = nullcontext()
    if operator_status == "completed_empty":
        stale = (
            local_root
            / "content_inbox"
            / "operator_events"
            / PROJECT
            / DATE
            / "stale.json"
        )
        stale.parent.mkdir(parents=True, exist_ok=True)
        stale.write_text("stale", encoding="utf-8")
        event_patch = patch.object(OperatorEventExporter, "build_documents", return_value=[])
    elif operator_status == "failed":
        event_patch = patch.object(
            OperatorEventExporter,
            "build_documents",
            side_effect=RuntimeError("synthetic shadow failure"),
        )
    elif operator_status != "ready":
        raise ValueError(operator_status)

    with (
        patch.dict(os.environ, environment, clear=False),
        patch("agent_content.cli.CodexTopicExporter") as topic_exporter,
        patch("agent_content.cli.CodexStoryExporter") as story_exporter,
        event_patch,
    ):
        topic_exporter.return_value.build_documents.return_value = [topic]
        story_exporter.return_value.build_documents.return_value = [story]
        result, _ = _write_topic_inbox([object()], prepare_sync_receipt=True)
    return result, topic, story


def _transaction_script(result: dict[str, object], token: str = "a" * 32) -> str:
    remote = _remote_release_paths("/opt/naz", token)
    return _remote_release_transaction_script(remote, result["sync_receipt"])


def _script_stdin(mock_call: object) -> str:
    """Return the script transported over stdin, never from SSH argv."""

    value = mock_call.kwargs.get("input")
    if value is None:
        value = mock_call.kwargs.get("input_bytes")
    if not isinstance(value, bytes):
        raise AssertionError("expected a byte script in subprocess stdin")
    return value.decode("utf-8")


def _posix_sh() -> str:
    shell = shutil.which("sh")
    if shell:
        return shell
    git = shutil.which("git")
    if git:
        candidate = Path(git).resolve().parent.parent / "usr" / "bin" / "sh.exe"
        if candidate.is_file():
            return str(candidate)
    raise AssertionError("a POSIX sh executable is required for the transport syntax gate")


def _simulate_remote_release(
    *,
    initial_roots: tuple[bool, bool] = (True, True),
    fail_at: str | None = None,
    rollback_fail_at: str | None = None,
    completed_empty: bool = False,
) -> dict[str, object]:
    """Independent test-only model of the required two-root transaction.

    The sequence below is deliberately literal.  It does not inspect either
    production plan constant, so a shared renderer/simulator defect cannot make
    these behavioral expectations pass accidentally.
    """

    subjects = ("markdown", "events")
    targets = {
        subject: "old"
        for subject, exists in zip(subjects, initial_roots, strict=True)
        if exists
    }
    original_targets = dict(targets)
    recovery: dict[str, str] = {}
    holds: dict[str, str] = {}
    failed_new: dict[str, str] = {}
    installed_started: set[str] = set()
    park_failed: set[str] = set()
    trace = ["lock_owned"]

    def outcome(
        *, reason: str | None, exit_code: int, committed: bool, lock_released: bool,
    ) -> dict[str, object]:
        return {
            "targets": dict(targets),
            "original_targets": original_targets,
            "recovery": dict(recovery),
            "holds": dict(holds),
            "failed_new": dict(failed_new),
            "lock_owned": not lock_released,
            "lock_released": lock_released,
            "committed": committed,
            "reason": reason,
            "exit_code": exit_code,
            "trace": tuple(trace),
        }

    # Remote tools and both staged trees are validated before recovery or swap.
    if fail_at and (
        fail_at.startswith("tool:")
        or fail_at in {"find", "traversal", "absent_stage", "extra_stage_file"}
    ):
        trace.append(f"preflight_failed:{fail_at}")
        trace.append("lock_released")
        return outcome(
            reason="vps_sync_remote_transaction_failed",
            exit_code=1,
            committed=False,
            lock_released=True,
        )

    # Recovery candidates for the pair are complete before any target moves.
    for subject in subjects:
        if fail_at == f"recovery:{subject}":
            trace.append(f"recovery_failed:{subject}")
            recovery.clear()
            trace.append("lock_released")
            return outcome(
                reason="vps_sync_remote_transaction_failed",
                exit_code=1,
                committed=False,
                lock_released=True,
            )
        recovery[subject] = targets.get(subject, "absent")
        trace.append(f"recovery_ready:{subject}")

    trace.append("swap_started")
    transaction_failed = False

    # Literal hold-markdown, hold-events sequence.
    for subject in subjects:
        if subject in targets:
            holds[subject] = targets.pop(subject)
            trace.append(f"held:{subject}")
        if fail_at == f"after_hold:{subject}":
            trace.append(f"failed_after_hold:{subject}")
            transaction_failed = True
            break

    # Literal install-markdown, install-events sequence.
    if not transaction_failed:
        for subject in subjects:
            installed_started.add(subject)
            trace.append(f"install_started:{subject}")
            if fail_at == f"install:{subject}":
                trace.append(f"install_failed:{subject}")
                transaction_failed = True
                break
            targets[subject] = "empty" if completed_empty and subject == "events" else "new"
            trace.append(f"installed:{subject}")
            if fail_at == f"after_install:{subject}":
                trace.append(f"failed_after_install:{subject}")
                transaction_failed = True
                break

    if not transaction_failed and fail_at == "postinstall_mismatch":
        targets["events"] = "corrupt-new"
        trace.append("postinstall_hash_mismatch")
        transaction_failed = True

    if not transaction_failed:
        trace.append("committed")
        if fail_at == "commit_cleanup":
            trace.append("commit_cleanup_failed")
            return outcome(
                reason="vps_sync_remote_commit_cleanup_failed",
                exit_code=76,
                committed=True,
                lock_released=False,
            )
        holds.clear()
        trace.append("lock_released")
        return outcome(reason=None, exit_code=0, committed=True, lock_released=True)

    rollback_failed = False

    # Park installed candidates instead of deleting them.
    for subject in subjects:
        if subject in installed_started and subject in targets:
            if rollback_fail_at == f"rollback_park_mv:{subject}":
                trace.append(f"park_failed:{subject}")
                park_failed.add(subject)
                rollback_failed = True
            else:
                failed_new[subject] = targets.pop(subject)
                trace.append(f"parked_new:{subject}")

    # Restore from old-hold, then from the independently prepared recovery copy.
    for subject in subjects:
        if subject in park_failed:
            trace.append(f"restore_blocked_by_live_new:{subject}")
        elif recovery[subject] == "absent" and subject not in targets:
            trace.append(f"absence_recovered:{subject}")
        elif targets.get(subject) == recovery[subject]:
            trace.append(f"already_recovered:{subject}")
        elif subject in holds:
            if rollback_fail_at in {
                f"rollback_restore_mv:{subject}",
                f"rollback_restore_both:{subject}",
            }:
                trace.append(f"restore_mv_failed:{subject}")
                if rollback_fail_at == f"rollback_restore_both:{subject}":
                    trace.append(f"restore_copy_failed:{subject}")
                    rollback_failed = True
                else:
                    targets[subject] = recovery[subject]
                    trace.append(f"restored_by_copy:{subject}")
            else:
                targets[subject] = holds.pop(subject)
                trace.append(f"restored_by_mv:{subject}")
        else:
            if recovery[subject] == "absent":
                trace.append(f"absence_recovered:{subject}")
            else:
                targets[subject] = recovery[subject]
                trace.append(f"restored_by_copy:{subject}")

        if rollback_fail_at == f"rollback_diff:{subject}":
            trace.append(f"rollback_diff_failed:{subject}")
            rollback_failed = True
        elif recovery[subject] == "absent" and subject not in targets:
            trace.append(f"rollback_verified:{subject}")
        elif targets.get(subject) != recovery[subject]:
            rollback_failed = True
        else:
            trace.append(f"rollback_verified:{subject}")

    if rollback_failed:
        trace.append("recovery_required")
        return outcome(
            reason="vps_sync_remote_recovery_required",
            exit_code=75,
            committed=False,
            lock_released=False,
        )

    if rollback_fail_at == "rollback_rm":
        trace.extend(("rollback_cleanup_failed", "operator_cleanup_required"))
        return outcome(
            reason="vps_sync_remote_cleanup_failed",
            exit_code=79,
            committed=False,
            lock_released=False,
        )

    trace.extend(("rollback_pair_verified", "lock_released"))
    return outcome(
        reason="vps_sync_remote_transaction_failed",
        exit_code=1,
        committed=False,
        lock_released=True,
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


class VpsReleaseSyncTests(unittest.TestCase):
    HOST = "deploy@example.test"
    VPS_PATH = "/opt/naz"

    def _sync_with_mock(self, result: dict[str, object]):
        runner = patch("agent_content.cli._run_checked")
        mocked = runner.start()
        self.addCleanup(runner.stop)
        _sync_nazai_release_to_vps(result, self.HOST, self.VPS_PATH)
        return mocked

    def test_no_sync_flag_makes_zero_network_calls(self) -> None:
        args = SimpleNamespace(sync_vps=False)
        with patch("agent_content.cli._sync_nazai_release_to_vps") as sync:
            self.assertFalse(_sync_vps_if_requested(args, {}))
        sync.assert_not_called()

    def test_incomplete_sidecar_blocks_network_before_first_subprocess(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result, _, _ = _write_release_fixture(Path(tmp), operator_status="failed")
            with patch("agent_content.cli._run_checked") as run_checked:
                with self.assertRaises(VpsSyncError) as raised:
                    _sync_nazai_release_to_vps(result, self.HOST, self.VPS_PATH)
        self.assertEqual(
            "vps_sync_operator_events_current_run_failed",
            raised.exception.reason_code,
        )
        run_checked.assert_not_called()

    def test_completed_empty_export_is_not_confused_with_failed_export(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            empty, _, _ = _write_release_fixture(
                Path(tmp) / "empty", operator_status="completed_empty"
            )
            failed, _, _ = _write_release_fixture(
                Path(tmp) / "failed", operator_status="failed"
            )
            self.assertEqual("completed_empty", empty["operator_events_status"])
            self.assertEqual((), empty["sync_receipt"]["operator_events_inventory"])
            self.assertEqual([], list(Path(empty["operator_events_dir"]).rglob("*")))
            with patch("agent_content.cli._run_checked") as run_checked:
                _sync_nazai_release_to_vps(empty, self.HOST, self.VPS_PATH)
                self.assertEqual(4, run_checked.call_count)
            with patch("agent_content.cli._run_checked") as run_checked:
                with self.assertRaises(VpsSyncError):
                    _sync_nazai_release_to_vps(failed, self.HOST, self.VPS_PATH)
                run_checked.assert_not_called()

    def test_invalid_operator_event_json_blocks_network(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result, _, _ = _write_release_fixture(Path(tmp))
            event_path = next(Path(result["operator_events_dir"]).rglob("*.json"))
            event_path.write_text("not-json", encoding="utf-8")
            with patch("agent_content.cli._run_checked") as run_checked:
                with self.assertRaises(VpsSyncError) as raised:
                    _sync_nazai_release_to_vps(result, self.HOST, self.VPS_PATH)
        self.assertEqual("vps_sync_operator_events_json_invalid", raised.exception.reason_code)
        run_checked.assert_not_called()

    def test_unsafe_host_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result, _, _ = _write_release_fixture(Path(tmp))
            with patch("agent_content.cli._run_checked") as run_checked:
                with self.assertRaises(VpsSyncError) as raised:
                    _sync_nazai_release_to_vps(
                        result, "deploy@example.test; touch owned", self.VPS_PATH
                    )
        self.assertEqual("vps_sync_host_invalid", raised.exception.reason_code)
        run_checked.assert_not_called()

    def test_unsafe_remote_path_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result, _, _ = _write_release_fixture(Path(tmp))
            with patch("agent_content.cli._run_checked") as run_checked:
                with self.assertRaises(VpsSyncError) as raised:
                    _sync_nazai_release_to_vps(result, self.HOST, "/opt/naz;touch-owned")
        self.assertEqual("vps_sync_path_invalid", raised.exception.reason_code)
        run_checked.assert_not_called()

    def test_ssh_uses_batch_mode(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result, _, _ = _write_release_fixture(Path(tmp))
            run_checked = self._sync_with_mock(result)
        commands = [call.args[0] for call in run_checked.call_args_list]
        self.assertTrue(commands)
        self.assertTrue(all("BatchMode=yes" in command for command in commands))

    def test_ssh_has_bounded_connect_timeout(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result, _, _ = _write_release_fixture(Path(tmp))
            run_checked = self._sync_with_mock(result)
        commands = [call.args[0] for call in run_checked.call_args_list]
        self.assertTrue(all("ConnectTimeout=15" in command for command in commands))

    def test_unknown_host_cannot_trigger_interactive_prompt(self) -> None:
        completed = SimpleNamespace(returncode=0)
        with tempfile.TemporaryDirectory() as tmp:
            result, _, _ = _write_release_fixture(Path(tmp))
            with patch("agent_content.cli.subprocess.run", return_value=completed) as run:
                _sync_nazai_release_to_vps(result, "unknown.example.test", self.VPS_PATH)
        self.assertEqual(4, run.call_count)
        for call in run.call_args_list:
            command = call.args[0]
            self.assertIn("BatchMode=yes", command)
            self.assertIn("StrictHostKeyChecking=yes", command)
            if command[0] == "ssh":
                self.assertEqual(["sh", "-s"], command[-2:])
                self.assertIsInstance(call.kwargs.get("input"), bytes)
                self.assertNotIn("stdin", call.kwargs)
                self.assertNotIn("text", call.kwargs)
                self.assertNotIn("encoding", call.kwargs)
                self.assertNotIn("errors", call.kwargs)
            else:
                self.assertEqual(subprocess.DEVNULL, call.kwargs["stdin"])
                self.assertNotIn("input", call.kwargs)
            self.assertLessEqual(call.kwargs["timeout"], 180)

    def test_remote_scripts_use_lf_only_bytes_without_text_mode(self) -> None:
        completed = SimpleNamespace(returncode=0)
        with patch("agent_content.cli.subprocess.run", return_value=completed) as run:
            cli._run_remote_script(
                self.HOST,
                "first\r\nsecond\rthird\n",
                failure_code="vps_sync_remote_transaction_failed",
            )
        command = run.call_args.args[0]
        kwargs = run.call_args.kwargs
        self.assertEqual("ssh", command[0])
        self.assertEqual(["sh", "-s"], command[-2:])
        self.assertEqual(b"first\nsecond\nthird\n", kwargs["input"])
        self.assertNotIn(b"\r", kwargs["input"])
        self.assertIsInstance(kwargs["input"], bytes)
        self.assertNotIn("text", kwargs)
        self.assertNotIn("encoding", kwargs)
        self.assertNotIn("errors", kwargs)

    def test_remote_script_nul_fails_closed_before_subprocess(self) -> None:
        script = "echo ok\x00echo bad"
        output = io.StringIO()
        with (
            patch("agent_content.cli.subprocess.run") as run,
            redirect_stdout(output),
            redirect_stderr(output),
        ):
            with self.assertRaises(VpsSyncError) as raised:
                cli._run_remote_script(
                    self.HOST,
                    script,
                    failure_code="vps_sync_remote_transaction_failed",
                )
        self.assertEqual("vps_sync_remote_transaction_failed", raised.exception.reason_code)
        run.assert_not_called()
        self.assertNotIn(script, output.getvalue())

    def test_each_sync_uses_unique_staging_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result, _, _ = _write_release_fixture(Path(tmp))
            first = self._sync_with_mock(result)
            first_script = _script_stdin(first.call_args_list[-1])
            with patch("agent_content.cli._run_checked") as second:
                _sync_nazai_release_to_vps(result, self.HOST, self.VPS_PATH)
            second_script = _script_stdin(second.call_args_list[-1])
        first_ids = re.findall(r"\.agent_content\.staging-([0-9a-f]{32})", first_script)
        second_ids = re.findall(r"\.agent_content\.staging-([0-9a-f]{32})", second_script)
        self.assertEqual(1, len(set(first_ids)))
        self.assertEqual(1, len(set(second_ids)))
        self.assertNotEqual(first_ids[0], second_ids[0])

    def test_both_trees_are_staged_before_any_production_swap(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result, _, _ = _write_release_fixture(Path(tmp))
            run_checked = self._sync_with_mock(result)
        commands = [call.args[0] for call in run_checked.call_args_list]
        self.assertEqual(["ssh", "scp", "scp", "ssh"], [item[0] for item in commands])
        self.assertIn("agent_content", " ".join(commands[1]))
        self.assertIn("operator_events", " ".join(commands[2]))
        self.assertEqual(["sh", "-s"], commands[3][-2:])
        self.assertIn(
            "phase=install_markdown",
            _script_stdin(run_checked.call_args_list[3]),
        )

    def test_remote_lock_is_acquired_before_swap(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result, _, _ = _write_release_fixture(Path(tmp))
            script = _transaction_script(result)
        self.assertLess(script.index("phase=lock"), script.index("phase=hold_markdown"))
        self.assertLess(script.index("phase=hold_markdown"), script.index("phase=install_markdown"))
        self.assertIn('printf \'%s\\n\' "$transaction_id" > "$lock_owner"', script)

    def test_generated_transaction_script_passes_posix_syntax_gate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result, _, _ = _write_release_fixture(Path(tmp))
            script = _transaction_script(result)
            payload = cli._remote_script_bytes(
                script,
                failure_code="vps_sync_remote_transaction_failed",
            )
            script_path = Path(tmp) / "nazai-release-transaction.sh"
            script_path.write_bytes(payload)
            completed = subprocess.run(
                [_posix_sh(), "-n", str(script_path)],
                text=True,
                encoding="utf-8",
                errors="replace",
                capture_output=True,
                check=False,
            )
        self.assertNotIn("\r", script)
        self.assertNotIn(b"\r", payload)
        self.assertEqual(0, completed.returncode, completed.stderr)

    def test_foreign_lock_work_is_untouched_and_commit_marker_is_atomic(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result, _, _ = _write_release_fixture(Path(tmp))
            script = _transaction_script(result)
        finish = script[script.index("finish()") : script.index("trap finish EXIT")]
        foreign_lock_guard = (
            'if [ "$lock_created" -eq 1 ]; then\n'
            '            if owns_lock; then\n'
            '                rm -rf -- "$lock_work" || cleanup_failed=1'
        )
        self.assertIn(foreign_lock_guard, finish)
        self.assertIn('if exists "$lock"; then exit 73; fi', script)
        self.assertLess(
            script.index('if exists "$lock"; then exit 73; fi'),
            script.index('mkdir -- "$lock_work"'),
        )

        pending_write = script.index('> "$lock_commit_pending"')
        marker_move = script.index('mv -- "$lock_commit_pending" "$lock_commit"')
        committed_flag = script.index("committed=1", marker_move)
        self.assertLess(pending_write, marker_move)
        self.assertLess(marker_move, committed_flag)
        self.assertIn(
            'if [ "$committed" -eq 0 ] && owns_lock && [ -f "$lock_commit" ]; then',
            finish,
        )
        self.assertIn(
            'if [ "$commit_value" = "$transaction_id $run_id" ]; then', finish
        )

    def test_existing_lock_fails_closed(self) -> None:
        results = [
            SimpleNamespace(returncode=0),
            SimpleNamespace(returncode=0),
            SimpleNamespace(returncode=0),
            SimpleNamespace(returncode=73),
            SimpleNamespace(returncode=0),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            result, _, _ = _write_release_fixture(Path(tmp))
            with patch("agent_content.cli.subprocess.run", side_effect=results) as run:
                with self.assertRaises(VpsSyncError) as raised:
                    _sync_nazai_release_to_vps(result, self.HOST, self.VPS_PATH)
        self.assertEqual("vps_sync_remote_lock_busy", raised.exception.reason_code)
        self.assertEqual(5, run.call_count)
        cleanup = _script_stdin(run.call_args_list[-1])
        self.assertIn("$lock_owner", cleanup)
        self.assertIn("$lock_safe", cleanup)
        self.assertNotIn("rm -rf -- \"$lock\"", cleanup)

    def test_cleanup_removes_only_unique_stages_before_preserving_ownerless_lock(self) -> None:
        remote = _remote_release_paths(self.VPS_PATH, "f" * 32)
        with patch("agent_content.cli._run_checked") as run_checked:
            cli._cleanup_remote_transaction(self.HOST, remote)
        cleanup = _script_stdin(run_checked.call_args)
        active_owner_guard = (
            'if [ "$owner_value" = "$transaction_id" ] '
            '&& [ "$safe_value" = "$transaction_id" ]; then'
        )
        stage_cleanup = 'rm -rf -- "$markdown_staging" "$events_staging"'
        lock_branch = 'if ! exists "$lock"; then'
        self.assertIn(stage_cleanup, cleanup)
        self.assertIn(lock_branch, cleanup)
        self.assertLess(cleanup.index(stage_cleanup), cleanup.index(lock_branch))
        self.assertIn(active_owner_guard, cleanup)
        self.assertNotIn('rm -rf -- "$lock"', cleanup)
        self.assertEqual(1, cleanup.count('rm -f -- "$lock_owner"'))
        self.assertGreater(
            cleanup.index('rm -f -- "$lock_owner"'), cleanup.index(active_owner_guard)
        )
        self.assertGreater(cleanup.index(active_owner_guard), cleanup.index("safe_value="))

    def test_markdown_upload_failure_leaves_targets_untouched(self) -> None:
        commands: list[tuple[list[str], dict[str, object]]] = []

        def fail_markdown(args: list[str], **kwargs: object) -> None:
            commands.append((args, kwargs))
            if args[0] == "scp":
                raise VpsSyncError("vps_sync_markdown_upload_failed")

        with tempfile.TemporaryDirectory() as tmp:
            result, _, _ = _write_release_fixture(Path(tmp))
            with patch("agent_content.cli._run_checked", side_effect=fail_markdown):
                with self.assertRaises(VpsSyncError):
                    _sync_nazai_release_to_vps(result, self.HOST, self.VPS_PATH)
        self.assertFalse(
            any(
                "phase=install_markdown"
                in bytes(kwargs.get("input_bytes", b"")).decode("utf-8")
                for _, kwargs in commands
            )
        )

    def test_sidecar_upload_failure_leaves_targets_untouched(self) -> None:
        commands: list[tuple[list[str], dict[str, object]]] = []
        scp_count = 0

        def fail_sidecar(args: list[str], **kwargs: object) -> None:
            nonlocal scp_count
            commands.append((args, kwargs))
            if args[0] == "scp":
                scp_count += 1
                if scp_count == 2:
                    raise VpsSyncError("vps_sync_operator_events_upload_failed")

        with tempfile.TemporaryDirectory() as tmp:
            result, _, _ = _write_release_fixture(Path(tmp))
            with patch("agent_content.cli._run_checked", side_effect=fail_sidecar):
                with self.assertRaises(VpsSyncError):
                    _sync_nazai_release_to_vps(result, self.HOST, self.VPS_PATH)
        self.assertEqual(2, scp_count)
        self.assertFalse(
            any(
                "phase=install_markdown"
                in bytes(kwargs.get("input_bytes", b"")).decode("utf-8")
                for _, kwargs in commands
            )
        )

    def test_first_swap_failure_restores_both_old_roots(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result, _, _ = _write_release_fixture(Path(tmp))
            script = _transaction_script(result)
        finish = script[script.index("finish()") : script.index("trap finish EXIT")]
        self.assertLess(script.index("phase=hold_markdown"), script.index("phase=hold_operator_events"))
        self.assertLess(script.index("phase=hold_operator_events"), script.index("phase=install_markdown"))
        self.assertIn('mv -- "$markdown_target" "$markdown_failed_new"', finish)
        self.assertIn('mv -- "$events_target" "$events_failed_new"', finish)
        self.assertIn('mv -- "$markdown_old_hold" "$markdown_target"', finish)
        self.assertIn('mv -- "$events_old_hold" "$events_target"', finish)
        self.assertIn('cp -a -- "$markdown_recovery" "$markdown_target"', finish)
        self.assertIn('cp -a -- "$events_recovery" "$events_target"', finish)
        outcome = _simulate_remote_release(fail_at="install:markdown")
        self._assert_failed_swap_outcome(outcome)

    def test_second_swap_failure_restores_both_old_roots(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result, _, _ = _write_release_fixture(Path(tmp))
            script = _transaction_script(result)
        finish = script[script.index("finish()") : script.index("trap finish EXIT")]
        self.assertNotIn('rm -rf -- "$markdown_target"', finish)
        self.assertNotIn('rm -rf -- "$events_target"', finish)
        self.assertIn('mv -- "$markdown_target" "$markdown_failed_new"', finish)
        self.assertIn('mv -- "$events_target" "$events_failed_new"', finish)
        self.assertIn('test -d "$markdown_target"', finish)
        self.assertIn('test -d "$events_target"', finish)
        outcome = _simulate_remote_release(fail_at="install:events")
        self._assert_failed_swap_outcome(outcome)

    def _assert_failed_swap_outcome(self, outcome: dict[str, object]) -> None:
        targets = outcome["targets"]
        recovery = outcome["recovery"]
        trace = outcome["trace"]
        self.assertNotEqual(0, outcome["exit_code"])
        self.assertEqual({"markdown", "events"}, set(targets))
        self.assertEqual(targets, recovery)
        self.assertEqual({}, outcome["holds"])
        self.assertTrue(outcome["lock_released"])
        self.assertIn("rollback_pair_verified", trace)
        self.assertLess(trace.index("rollback_pair_verified"), trace.index("lock_released"))

    def test_failed_swap_does_not_delete_only_recovery_backup(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result, _, _ = _write_release_fixture(Path(tmp))
            script = _transaction_script(result)
        self.assertIn('cp -a -- "$markdown_target" "$markdown_recovery"', script)
        self.assertIn('cp -a -- "$events_target" "$events_recovery"', script)
        finish = script[script.index("finish()") : script.index("trap finish EXIT")]
        rollback = finish[finish.index('elif [ "$swap_started" -eq 1 ]') :]
        self.assertIn('[ "$markdown_recovery_ready" -ne 1 ]', rollback)
        self.assertIn('[ "$events_recovery_ready" -ne 1 ]', rollback)
        self.assertIn('exit 75', rollback)
        self.assertNotIn('rm -rf -- "$markdown_target"', rollback)
        self.assertNotIn('rm -rf -- "$events_target"', rollback)

    def test_successful_swap_installs_both_new_roots(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result, _, _ = _write_release_fixture(Path(tmp))
            script = _transaction_script(result)
        markdown_install = script.index('mv -- "$markdown_staging" "$markdown_target"')
        events_install = script.index('mv -- "$events_staging" "$events_target"')
        markdown_armed = script.index(
            "markdown_install_started=1", script.index("phase=install_markdown")
        )
        events_armed = script.index(
            "events_install_started=1", script.index("phase=install_operator_events")
        )
        markdown_done = script.index("markdown_installed=1", markdown_install)
        events_done = script.index("events_installed=1", events_install)
        verify = script.index("phase=verify_install")
        committed = script.index("phase=committed")
        self.assertLess(markdown_armed, markdown_install)
        self.assertLess(markdown_install, markdown_done)
        self.assertLess(markdown_done, events_armed)
        self.assertLess(events_armed, events_install)
        self.assertLess(events_install, events_done)
        self.assertLess(events_done, verify)
        self.assertLess(verify, committed)
        self.assertIn("sha256sum", script[verify:committed])

    def test_successful_swap_releases_own_lock(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result, _, _ = _write_release_fixture(Path(tmp))
            script = _transaction_script(result)
        self.assertIn("owns_lock || return 1", script)
        self.assertIn("may_release_lock || return 1", script)
        self.assertIn('rm -f -- "$lock_safe"', script)
        self.assertIn('rm -f -- "$lock_owner"', script)
        self.assertIn('rmdir -- "$lock"', script)

    def test_failed_sync_returns_nonzero(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result, topic, _ = _write_release_fixture(Path(tmp))
            args = SimpleNamespace(
                config=None,
                sync_vps=True,
                date=DATE,
                vps_host=self.HOST,
                vps_path=self.VPS_PATH,
            )
            output = io.StringIO()
            with (
                patch("agent_content.cli.load_config", return_value={}),
                patch(
                    "agent_content.cli._refresh_codex_summaries",
                    return_value=[SimpleNamespace(date=DATE)],
                ),
                patch("agent_content.cli._write_topic_inbox", return_value=(result, [topic])),
                patch(
                    "agent_content.cli._sync_vps_if_requested",
                    side_effect=VpsSyncError("vps_sync_stage_init_failed"),
                ),
                redirect_stdout(output),
            ):
                return_code = cli.run_export_nazai_inbox(args)
        self.assertEqual(1, return_code)
        self.assertIn("vps_sync_stage_init_failed", output.getvalue())
        self.assertNotIn(self.HOST, output.getvalue())
        self.assertNotIn(self.VPS_PATH, output.getvalue())

    def test_local_exports_remain_after_remote_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result, _, _ = _write_release_fixture(Path(tmp))
            markdown = next(Path(result["inbox_dir"]).rglob("*.md"))
            event = next(Path(result["operator_events_dir"]).rglob("*.json"))
            before = (markdown.read_bytes(), event.read_bytes())
            results = [SimpleNamespace(returncode=21), SimpleNamespace(returncode=0)]
            with patch("agent_content.cli.subprocess.run", side_effect=results):
                with self.assertRaises(VpsSyncError):
                    _sync_nazai_release_to_vps(result, self.HOST, self.VPS_PATH)
            self.assertEqual(before, (markdown.read_bytes(), event.read_bytes()))

    def test_fixed_shared_staging_and_backup_names_are_not_used(self) -> None:
        first = _remote_release_paths(self.VPS_PATH, "1" * 32)
        second = _remote_release_paths(self.VPS_PATH, "2" * 32)
        for key in (
            "markdown_staging",
            "events_staging",
            "markdown_old_hold",
            "events_old_hold",
            "markdown_recovery",
            "events_recovery",
        ):
            self.assertNotEqual(first[key], second[key])
            self.assertTrue(first[key].endswith("1" * 32))
        self.assertNotIn("markdown_backup", first)
        self.assertNotIn("events_backup", first)

    def test_command_arguments_do_not_allow_shell_injection(self) -> None:
        malicious = (
            ("host;id", self.VPS_PATH),
            ("-oProxyCommand=id", self.VPS_PATH),
            (self.HOST, "/opt/naz$(id)"),
            (self.HOST, "/opt/naz/../root"),
        )
        with tempfile.TemporaryDirectory() as tmp:
            result, _, _ = _write_release_fixture(Path(tmp))
            for host, path in malicious:
                with self.subTest(host=host, path=path):
                    with patch("agent_content.cli._run_checked") as run_checked:
                        with self.assertRaises(VpsSyncError):
                            _sync_nazai_release_to_vps(result, host, path)
                        run_checked.assert_not_called()

    def test_relative_dash_prefixed_local_source_is_absolute_scp_operand(self) -> None:
        original_cwd = Path.cwd()
        with tempfile.TemporaryDirectory() as tmp:
            os.chdir(tmp)
            try:
                result, _, _ = _write_release_fixture(Path("-dash-source"))
                self.assertFalse(Path(result["inbox_dir"]).is_absolute())
                self.assertTrue(str(result["inbox_dir"]).startswith("-"))
                with patch("agent_content.cli._run_checked") as run_checked:
                    _sync_nazai_release_to_vps(result, self.HOST, self.VPS_PATH)
            finally:
                os.chdir(original_cwd)

        scp_commands = [
            call.args[0]
            for call in run_checked.call_args_list
            if call.args[0][0] == "scp"
        ]
        self.assertEqual(2, len(scp_commands))
        sources = [command[-2] for command in scp_commands]
        self.assertTrue(all(Path(source).is_absolute() for source in sources))
        self.assertTrue(all(not source.startswith("-") for source in sources))
        live_roots = {
            result["sync_receipt"]["markdown_root"],
            result["sync_receipt"]["operator_events_root"],
        }
        self.assertTrue(set(sources).isdisjoint(live_roots))
        self.assertEqual(1, len({str(Path(source).parent) for source in sources}))

    def test_same_count_stale_tree_fails_receipt_hash(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result, _, _ = _write_release_fixture(Path(tmp))
            markdown = next(Path(result["inbox_dir"]).rglob("*.md"))
            raw = bytearray(markdown.read_bytes())
            raw[-2] = raw[-2] ^ 1
            markdown.write_bytes(bytes(raw))
            with patch("agent_content.cli._run_checked") as run_checked:
                with self.assertRaises(VpsSyncError) as raised:
                    _sync_nazai_release_to_vps(result, self.HOST, self.VPS_PATH)
        self.assertEqual("vps_sync_markdown_current_run_mismatch", raised.exception.reason_code)
        run_checked.assert_not_called()

    def test_receipt_is_bound_to_exact_resolved_roots(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result, _, _ = _write_release_fixture(Path(tmp))
            result["sync_receipt"]["markdown_root"] = str(Path(tmp) / "other")
            with patch("agent_content.cli._run_checked") as run_checked:
                with self.assertRaises(VpsSyncError) as raised:
                    _sync_nazai_release_to_vps(result, self.HOST, self.VPS_PATH)
        self.assertEqual("vps_sync_current_run_root_mismatch", raised.exception.reason_code)
        run_checked.assert_not_called()

    def test_sibling_partial_tree_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result, _, _ = _write_release_fixture(Path(tmp))
            artifact = Path(result["inbox_dir"]).parent / ".agent_content.staging-abandoned"
            artifact.mkdir()
            with patch("agent_content.cli._run_checked") as run_checked:
                with self.assertRaises(VpsSyncError) as raised:
                    _sync_nazai_release_to_vps(result, self.HOST, self.VPS_PATH)
        self.assertEqual("vps_sync_local_partial_artifact_present", raised.exception.reason_code)
        run_checked.assert_not_called()

    def test_remote_stage_validation_checks_shape_symlinks_and_hashes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result, _, _ = _write_release_fixture(Path(tmp))
            script = _transaction_script(result)
        self.assertIn(
            'if ! find "$markdown_staging" -mindepth 1 -type l -print > "$scan_links"; then exit 78; fi',
            script,
        )
        self.assertIn(
            'if ! find "$events_staging" -mindepth 1 -print > "$scan_all"; then exit 78; fi',
            script,
        )
        self.assertIn("! -type d ! -type f", script)
        self.assertIn('if ! wc -l < "$scan_files" > "$scan_count"; then exit 78; fi', script)
        self.assertIn('if ! actual_file_count=$(tr -d "[:space:]"', script)
        self.assertIn("sha256sum --", script)
        for _, digest in result["sync_receipt"]["markdown_inventory"]:
            self.assertIn(digest, script)
        for _, digest in result["sync_receipt"]["operator_events_inventory"]:
            self.assertIn(digest, script)

    def test_independent_first_rollout_states_prepare_pair_before_swap(self) -> None:
        states = {
            "markdown_only": (True, False),
            "events_only": (False, True),
            "both_absent": (False, False),
        }
        for name, initial in states.items():
            with self.subTest(state=name, outcome="success"):
                completed = _simulate_remote_release(initial_roots=initial)
                self.assertEqual({"markdown": "new", "events": "new"}, completed["targets"])
                self.assertTrue(completed["committed"])
                self.assertTrue(completed["lock_released"])
            with self.subTest(state=name, outcome="second_recovery_fails"):
                failed = _simulate_remote_release(
                    initial_roots=initial,
                    fail_at="recovery:events",
                )
                self.assertEqual(failed["original_targets"], failed["targets"])
                self.assertNotIn("swap_started", failed["trace"])
                self.assertEqual({}, failed["recovery"])
                self.assertTrue(failed["lock_released"])
                self.assertNotEqual(0, failed["exit_code"])

    def test_independent_failures_after_holds_and_installs_restore_pair(self) -> None:
        failures = (
            "after_hold:markdown",
            "after_hold:events",
            "install:markdown",
            "install:events",
            "after_install:markdown",
            "postinstall_mismatch",
        )
        for failure in failures:
            with self.subTest(failure=failure):
                outcome = _simulate_remote_release(fail_at=failure)
                self.assertEqual(
                    {"markdown": "old", "events": "old"},
                    outcome["targets"],
                )
                self.assertEqual(outcome["targets"], outcome["recovery"])
                self.assertEqual({}, outcome["holds"])
                self.assertEqual("vps_sync_remote_transaction_failed", outcome["reason"])
                self.assertTrue(outcome["lock_released"])
                self.assertIn("rollback_pair_verified", outcome["trace"])

    def test_independent_absent_root_remains_absent_after_rollback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result, _, _ = _write_release_fixture(Path(tmp))
            script = _transaction_script(result)
        self.assertIn(
            'validate_empty "$markdown_recovery" "markdown-rollback-empty"', script
        )
        self.assertIn(
            'validate_empty "$events_recovery" "events-rollback-empty"', script
        )
        self.assertNotIn("$markdown-rollback-empty", script)
        self.assertNotIn("$events-rollback-empty", script)

        for initial in ((True, False), (False, True), (False, False)):
            with self.subTest(initial=initial):
                outcome = _simulate_remote_release(
                    initial_roots=initial,
                    fail_at="after_install:events",
                )
                expected = {
                    subject: "old"
                    for subject, existed in zip(
                        ("markdown", "events"), initial, strict=True
                    )
                    if existed
                }
                self.assertEqual(expected, outcome["targets"])
                self.assertTrue(outcome["lock_released"])
                self.assertIn("rollback_pair_verified", outcome["trace"])

    def test_independent_rollback_failures_require_recovery_and_keep_artifacts(self) -> None:
        failures = (
            "rollback_park_mv:markdown",
            "rollback_diff:markdown",
        )
        for rollback_failure in failures:
            with self.subTest(failure=rollback_failure):
                outcome = _simulate_remote_release(
                    fail_at="install:events",
                    rollback_fail_at=rollback_failure,
                )
                self.assertEqual("vps_sync_remote_recovery_required", outcome["reason"])
                self.assertEqual(75, outcome["exit_code"])
                self.assertTrue(outcome["lock_owned"])
                self.assertFalse(outcome["lock_released"])
                self.assertEqual(
                    {"markdown": "old", "events": "old"},
                    outcome["recovery"],
                )
                self.assertTrue(outcome["holds"] or outcome["failed_new"])

        cleanup = _simulate_remote_release(
            fail_at="install:events",
            rollback_fail_at="rollback_rm",
        )
        self.assertEqual("vps_sync_remote_cleanup_failed", cleanup["reason"])
        self.assertEqual(79, cleanup["exit_code"])
        self.assertTrue(cleanup["lock_owned"])
        self.assertEqual(
            {"markdown": "old", "events": "old"}, cleanup["targets"]
        )
        self.assertTrue(cleanup["failed_new"])
        self.assertTrue(cleanup["recovery"])

    def test_restore_mv_failure_uses_fallback_copy(self) -> None:
        outcome = _simulate_remote_release(
            fail_at="install:events",
            rollback_fail_at="rollback_restore_mv:markdown",
        )
        self.assertEqual({"markdown": "old", "events": "old"}, outcome["targets"])
        self.assertIn("restore_mv_failed:markdown", outcome["trace"])
        self.assertIn("restored_by_copy:markdown", outcome["trace"])
        self.assertTrue(outcome["lock_released"])

    def test_second_install_and_both_restore_methods_failure_needs_operator(self) -> None:
        outcome = _simulate_remote_release(
            fail_at="install:events",
            rollback_fail_at="rollback_restore_both:markdown",
        )
        self.assertEqual("vps_sync_remote_recovery_required", outcome["reason"])
        self.assertEqual(75, outcome["exit_code"])
        self.assertTrue(outcome["lock_owned"])
        self.assertFalse(outcome["lock_released"])
        self.assertIn("markdown", outcome["recovery"])
        self.assertIn("markdown", outcome["holds"])
        self.assertIn("markdown", outcome["failed_new"])
        self.assertIn("restore_mv_failed:markdown", outcome["trace"])
        self.assertIn("restore_copy_failed:markdown", outcome["trace"])
        self.assertNotIn("rollback_pair_verified", outcome["trace"])

    def test_commit_cleanup_failure_keeps_new_pair_and_last_good_recovery(self) -> None:
        outcome = _simulate_remote_release(fail_at="commit_cleanup")
        self.assertEqual({"markdown": "new", "events": "new"}, outcome["targets"])
        self.assertEqual({"markdown": "old", "events": "old"}, outcome["recovery"])
        self.assertEqual("vps_sync_remote_commit_cleanup_failed", outcome["reason"])
        self.assertEqual(76, outcome["exit_code"])
        self.assertTrue(outcome["committed"])
        self.assertTrue(outcome["lock_owned"])
        self.assertEqual({"markdown": "old", "events": "old"}, outcome["holds"])

    def test_completed_empty_and_remote_preflight_fail_closed_behavior(self) -> None:
        completed = _simulate_remote_release(completed_empty=True)
        self.assertEqual("empty", completed["targets"]["events"])
        self.assertTrue(completed["committed"])

        failures = (
            "tool:find",
            "tool:sha256sum",
            "find",
            "traversal",
            "absent_stage",
            "extra_stage_file",
        )
        for failure in failures:
            with self.subTest(failure=failure):
                outcome = _simulate_remote_release(fail_at=failure)
                self.assertEqual(outcome["original_targets"], outcome["targets"])
                self.assertNotIn("swap_started", outcome["trace"])
                self.assertFalse(outcome["committed"])
                self.assertNotEqual(0, outcome["exit_code"])

    def test_remote_script_checks_tools_and_each_traversal_status_before_swap(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            completed_empty, _, _ = _write_release_fixture(
                Path(tmp), operator_status="completed_empty"
            )
            script = _transaction_script(completed_empty)
        tools = "sh find wc tr sha256sum diff cp mv rm mkdir rmdir cat"
        self.assertIn(f"for required_tool in {tools}; do", script)
        self.assertLess(script.index("for required_tool"), script.index("phase=lock"))
        self.assertIn('test "$actual_file_count" -eq 0', script)
        self.assertIn('test "$actual_entry_count" -eq 0', script)
        self.assertIn('test -d "$events_staging"', script)
        for line in script.splitlines():
            stripped = line.strip()
            if "find \"$" in stripped or "wc -l" in stripped or "tr -d" in stripped:
                self.assertTrue(
                    stripped.startswith("if ! ") or "$(" in stripped,
                    stripped,
                )

    def test_remote_exit_codes_and_timeout_are_stable(self) -> None:
        mappings = {
            75: "vps_sync_remote_recovery_required",
            76: "vps_sync_remote_commit_cleanup_failed",
            79: "vps_sync_remote_cleanup_failed",
        }
        for returncode, reason in mappings.items():
            with self.subTest(returncode=returncode):
                with patch(
                    "agent_content.cli.subprocess.run",
                    return_value=SimpleNamespace(returncode=returncode),
                ):
                    with self.assertRaises(VpsSyncError) as raised:
                        cli._run_checked(
                            cli._ssh_args(self.HOST),
                            failure_code="vps_sync_remote_transaction_failed",
                            exit_reason_codes=mappings,
                            input_text="exit",
                        )
                self.assertEqual(reason, raised.exception.reason_code)

        timeout = subprocess.TimeoutExpired(cmd="ssh", timeout=180)
        with patch("agent_content.cli.subprocess.run", side_effect=timeout):
            with self.assertRaises(VpsSyncError) as raised:
                cli._run_checked(
                    cli._ssh_args(self.HOST),
                    failure_code="vps_sync_remote_transaction_failed",
                    input_text="exit",
                )
        self.assertEqual("vps_sync_remote_timeout", raised.exception.reason_code)

        with tempfile.TemporaryDirectory() as tmp:
            result, _, _ = _write_release_fixture(Path(tmp))
            markdown = next(Path(result["inbox_dir"]).rglob("*.md"))
            event = next(Path(result["operator_events_dir"]).rglob("*.json"))
            local_bytes = (markdown.read_bytes(), event.read_bytes())
            with patch("agent_content.cli.subprocess.run", side_effect=timeout):
                with self.assertRaises(VpsSyncError) as raised:
                    _sync_nazai_release_to_vps(result, self.HOST, self.VPS_PATH)
            self.assertEqual(
                "vps_sync_remote_timeout", raised.exception.reason_code
            )
            self.assertEqual(local_bytes, (markdown.read_bytes(), event.read_bytes()))

    def test_large_unicode_hostile_inventory_uses_fixed_ssh_argv_and_stdin(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result, _, _ = _write_release_fixture(Path(tmp))
            folder = Path(result["inbox_dir"]) / PROJECT / DATE
            names = [f"frame {index:02d} тест.md" for index in range(40)]
            names.append("-- $(echo pwned); 'юникод файл'.md")
            for index, name in enumerate(names):
                (folder / name).write_text(f"fixture-{index}", encoding="utf-8")
            result["document_count"] += len(names)
            result["sync_receipt"] = cli._build_current_run_receipt(result)
            self.assertGreaterEqual(
                len(result["sync_receipt"]["markdown_inventory"]), 30
            )
            with patch("agent_content.cli._run_checked") as run_checked:
                _sync_nazai_release_to_vps(result, self.HOST, self.VPS_PATH)

        ssh_calls = [
            call for call in run_checked.call_args_list if call.args[0][0] == "ssh"
        ]
        self.assertEqual(2, len(ssh_calls))
        for call in ssh_calls:
            argv = call.args[0]
            self.assertEqual(["sh", "-s"], argv[-2:])
            self.assertIn("BatchMode=yes", argv)
            self.assertIn("StrictHostKeyChecking=yes", argv)
            self.assertLess(len(" ".join(argv)), 400)
            self.assertNotIn("echo pwned", " ".join(argv))
            self.assertIsInstance(call.kwargs.get("input_bytes"), bytes)
        transaction = _script_stdin(ssh_calls[-1])
        self.assertIn("echo pwned", transaction)
        self.assertIn("юникод", transaction)

    def test_root_and_descendant_junctions_block_before_network(self) -> None:
        cases = (
            ("markdown_root", "inbox_dir", False, "vps_sync_markdown_result_invalid"),
            (
                "event_root",
                "operator_events_dir",
                False,
                "vps_sync_operator_events_result_invalid",
            ),
            ("markdown_child", "inbox_dir", True, "vps_sync_markdown_tree_invalid"),
            (
                "event_child",
                "operator_events_dir",
                True,
                "vps_sync_operator_events_tree_invalid",
            ),
        )
        for name, key, descendant, reason in cases:
            with self.subTest(case=name), tempfile.TemporaryDirectory() as tmp:
                result, _, _ = _write_release_fixture(Path(tmp))
                root = Path(result[key]).absolute()
                blocked = next(path for path in root.rglob("*") if path.is_file()) if descendant else root

                def junction(path: Path, *, target: Path = blocked) -> bool:
                    return Path(path).absolute() == target

                with (
                    patch("agent_content.cli._path_is_junction", side_effect=junction),
                    patch("agent_content.cli._run_checked") as run_checked,
                ):
                    with self.assertRaises(VpsSyncError) as raised:
                        _sync_nazai_release_to_vps(result, self.HOST, self.VPS_PATH)
                self.assertEqual(reason, raised.exception.reason_code)
                run_checked.assert_not_called()

    def test_file_attribute_reparse_root_and_descendant_block_before_snapshot(self) -> None:
        cases = (
            (False, "vps_sync_markdown_result_invalid"),
            (True, "vps_sync_markdown_tree_invalid"),
        )
        for descendant, reason in cases:
            with self.subTest(descendant=descendant), tempfile.TemporaryDirectory() as tmp:
                result, _, _ = _write_release_fixture(Path(tmp))
                root = Path(result["inbox_dir"])
                blocked = next(root.rglob("*.md")) if descendant else root
                blocked_inode = blocked.lstat().st_ino
                original = cli._stat_is_reparse

                def is_reparse(node: os.stat_result, *, inode: int = blocked_inode) -> bool:
                    return node.st_ino == inode or original(node)

                with (
                    patch("agent_content.cli._stat_is_reparse", side_effect=is_reparse),
                    patch("agent_content.cli._run_checked") as run_checked,
                ):
                    with self.assertRaises(VpsSyncError) as raised:
                        _sync_nazai_release_to_vps(result, self.HOST, self.VPS_PATH)
                self.assertEqual(reason, raised.exception.reason_code)
                run_checked.assert_not_called()

    def test_snapshot_allocation_failure_is_stable_and_network_free(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result, _, _ = _write_release_fixture(Path(tmp))
            with (
                patch("agent_content.cli.mkdtemp", side_effect=OSError("private path")),
                patch("agent_content.cli._run_checked") as run_checked,
            ):
                with self.assertRaises(VpsSyncError) as raised:
                    _sync_nazai_release_to_vps(result, self.HOST, self.VPS_PATH)
        self.assertEqual("vps_sync_local_snapshot_failed", raised.exception.reason_code)
        self.assertNotIn("private path", str(raised.exception))
        run_checked.assert_not_called()

    def test_live_mutation_after_snapshot_never_changes_uploaded_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result, _, _ = _write_release_fixture(Path(tmp))
            live_markdown = next(Path(result["inbox_dir"]).rglob("*.md"))
            sealed_bytes = live_markdown.read_bytes()
            unexpected = b"unexpected live mutation"
            original_stage = cli._stage_prepared_nazai_release
            uploaded: dict[str, tuple[bytes, ...]] = {}
            snapshot_sources: list[Path] = []

            def stage(snapshot_result: dict[str, object], host: str, path: str):
                live_markdown.write_bytes(unexpected)
                return original_stage(snapshot_result, host, path)

            def capture(args: list[str], **_: object) -> None:
                if args[0] != "scp":
                    return
                source = Path(args[-2])
                snapshot_sources.append(source)
                uploaded[source.name] = tuple(
                    file.read_bytes() for file in sorted(source.rglob("*")) if file.is_file()
                )

            with (
                patch("agent_content.cli._stage_prepared_nazai_release", side_effect=stage),
                patch("agent_content.cli._run_checked", side_effect=capture),
            ):
                _sync_nazai_release_to_vps(result, self.HOST, self.VPS_PATH)

            self.assertEqual(unexpected, live_markdown.read_bytes())
            self.assertIn(sealed_bytes, uploaded["markdown"])
            self.assertNotIn(unexpected, uploaded["markdown"])
            self.assertTrue(snapshot_sources)
            self.assertTrue(all(not source.exists() for source in snapshot_sources))

    def test_real_no_sync_cli_routes_do_not_prepare_transport_or_network(self) -> None:
        topic, story = _pair(messages=_proof_messages())
        summary = SimpleNamespace(date=DATE)
        routes = (
            (
                cli.run_export_nazai_inbox,
                SimpleNamespace(config=None, sync_vps=False, date=DATE),
            ),
            (
                cli.run_export_nazai_inbox_all,
                SimpleNamespace(config=None, sync_vps=False),
            ),
        )
        for route, args in routes:
            with self.subTest(route=route.__name__), tempfile.TemporaryDirectory() as tmp:
                environment = {
                    "NAZAI_LOCAL_PATH": str(Path(tmp) / "Naz path юникод"),
                    "NAZAI_INBOX_DIR": "content_inbox/agent_content",
                }
                output = io.StringIO()
                with (
                    patch.dict(os.environ, environment, clear=False),
                    patch("agent_content.cli.load_config", return_value={}),
                    patch("agent_content.cli._refresh_codex_summaries", return_value=[summary]),
                    patch("agent_content.cli.CodexTopicExporter") as topic_exporter,
                    patch("agent_content.cli.CodexStoryExporter") as story_exporter,
                    patch("agent_content.cli._build_current_run_receipt") as receipt,
                    patch("agent_content.cli._create_current_release_snapshot") as snapshot,
                    patch("agent_content.cli._run_checked") as run_checked,
                    patch("agent_content.cli.subprocess.run") as subprocess_run,
                    redirect_stdout(output),
                ):
                    topic_exporter.return_value.build_documents.return_value = [topic]
                    story_exporter.return_value.build_documents.return_value = [story]
                    return_code = route(args)
                self.assertEqual(0, return_code)
                self.assertIn(environment["NAZAI_LOCAL_PATH"], output.getvalue())
                receipt.assert_not_called()
                snapshot.assert_not_called()
                run_checked.assert_not_called()
                subprocess_run.assert_not_called()

        restore_args = SimpleNamespace(config=None, sync_vps=False)
        with tempfile.TemporaryDirectory() as tmp:
            with (
                patch("agent_content.cli.load_config", return_value={"outputs_dir": tmp}),
                patch("agent_content.cli._refresh_codex_summaries", return_value=[]),
                patch("agent_content.cli._discover_content_source_dates", return_value=[]),
                patch("agent_content.cli._build_current_run_receipt") as receipt,
                patch("agent_content.cli._create_current_release_snapshot") as snapshot,
                patch("agent_content.cli._run_checked") as run_checked,
                patch("agent_content.cli.subprocess.run") as subprocess_run,
            ):
                self.assertEqual(0, cli.run_restore_history(restore_args))
        receipt.assert_not_called()
        snapshot.assert_not_called()
        run_checked.assert_not_called()
        subprocess_run.assert_not_called()

    def test_restore_history_positive_local_only_keeps_path_bearing_output(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "Локальный путь УНИКАЛЬНЫЙ"
            args = SimpleNamespace(config=None, sync_vps=False)
            output = io.StringIO()
            rebuilt: list[Path] = []

            def write_rebuilt(path: Path, _pack: object) -> None:
                Path(path).write_text("local rebuild", encoding="utf-8")
                rebuilt.append(Path(path))

            with (
                patch("agent_content.cli.load_config", return_value={"outputs_dir": str(root)}),
                patch("agent_content.cli._refresh_codex_summaries", return_value=[SimpleNamespace(date=DATE)]),
                patch("agent_content.cli._discover_content_source_dates", return_value=[DATE]),
                patch("agent_content.cli._content_projects", return_value=[{}]),
                patch("agent_content.cli._build_pack_for_project", return_value=object()),
                patch("agent_content.cli._select_daily_pack", return_value=object()),
                patch("agent_content.cli.MarkdownWriter") as markdown_writer,
                patch("agent_content.cli.JsonWriter") as json_writer,
                patch("agent_content.cli.TodayPickWriter") as today_pick_writer,
                patch("agent_content.cli._build_current_run_receipt") as receipt,
                patch("agent_content.cli._create_current_release_snapshot") as snapshot,
                patch("agent_content.cli._run_checked") as run_checked,
                redirect_stdout(output),
            ):
                markdown_writer.return_value.write.side_effect = write_rebuilt
                json_writer.return_value.write.side_effect = write_rebuilt
                today_pick_writer.return_value.write.side_effect = write_rebuilt
                self.assertEqual(0, cli.run_restore_history(args))

            self.assertEqual(3, len(rebuilt))
            self.assertTrue(all(path.exists() for path in rebuilt))
            self.assertIn(str(root), output.getvalue())
            receipt.assert_not_called()
            snapshot.assert_not_called()
            run_checked.assert_not_called()

    def test_safe_cli_boundary_covers_receipt_snapshot_reparse_and_timeout(self) -> None:
        reasons = (
            "vps_sync_current_run_receipt_invalid",
            "vps_sync_local_snapshot_failed",
            "vps_sync_markdown_tree_invalid",
            "vps_sync_operator_events_tree_invalid",
            "vps_sync_remote_timeout",
        )
        for reason in reasons:
            with self.subTest(reason=reason), tempfile.TemporaryDirectory() as tmp:
                result, topic, _ = _write_release_fixture(Path(tmp))
                args = SimpleNamespace(
                    config=None,
                    sync_vps=True,
                    date=DATE,
                    vps_host=self.HOST,
                    vps_path=self.VPS_PATH,
                )
                output = io.StringIO()
                errors = io.StringIO()
                markdown = next(Path(result["inbox_dir"]).rglob("*.md"))
                event = next(Path(result["operator_events_dir"]).rglob("*.json"))
                local_bytes = (markdown.read_bytes(), event.read_bytes())
                private_detail = f"private-{tmp}-snapshot"
                failure = VpsSyncError(reason)
                failure.args = (private_detail,)
                with (
                    patch("agent_content.cli.load_config", return_value={}),
                    patch(
                        "agent_content.cli._refresh_codex_summaries",
                        return_value=[SimpleNamespace(date=DATE)],
                    ),
                    patch(
                        "agent_content.cli._write_topic_inbox_for_sync",
                        return_value=(result, [topic]),
                    ),
                    patch(
                        "agent_content.cli._sync_vps_if_requested",
                        side_effect=failure,
                    ),
                    redirect_stdout(output),
                    redirect_stderr(errors),
                ):
                    return_code = cli.run_export_nazai_inbox(args)
                rendered = output.getvalue() + errors.getvalue()
                self.assertEqual(1, return_code)
                self.assertIn(reason, rendered)
                self.assertNotIn(str(result["inbox_dir"]), rendered)
                self.assertNotIn(str(tmp), rendered)
                self.assertNotIn("C:\\", rendered)
                self.assertNotIn(self.HOST, rendered)
                self.assertNotIn(private_detail, rendered)
                self.assertNotIn("Traceback", rendered)
                self.assertEqual(local_bytes, (markdown.read_bytes(), event.read_bytes()))

    def test_all_and_restore_routes_share_safe_sync_error_boundary(self) -> None:
        reason = "vps_sync_current_run_export_missing"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "Локальный путь УНИКАЛЬНЫЙ"
            result, topic, _ = _write_release_fixture(root)
            markdown = next(Path(result["inbox_dir"]).rglob("*.md"))
            event = next(Path(result["operator_events_dir"]).rglob("*.json"))
            local_bytes = (markdown.read_bytes(), event.read_bytes())
            failure = VpsSyncError(reason)
            failure.args = (f"internal-{tmp}-snapshot",)

            all_args = SimpleNamespace(config=None, sync_vps=True)
            output = io.StringIO()
            errors = io.StringIO()
            with (
                patch("agent_content.cli.load_config", return_value={}),
                patch(
                    "agent_content.cli._refresh_codex_summaries",
                    return_value=[SimpleNamespace(date=DATE)],
                ),
                patch(
                    "agent_content.cli._write_topic_inbox_for_sync",
                    return_value=(result, [topic]),
                ),
                patch("agent_content.cli._sync_vps_if_requested", side_effect=failure),
                redirect_stdout(output),
                redirect_stderr(errors),
            ):
                self.assertEqual(1, cli.run_export_nazai_inbox_all(all_args))
            rendered = output.getvalue() + errors.getvalue()
            self.assertIn(reason, rendered)
            self.assertNotIn(str(root), rendered)
            self.assertNotIn("C:\\", rendered)
            self.assertNotIn(self.HOST, rendered)
            self.assertNotIn("internal-", rendered)
            self.assertNotIn("Traceback", rendered)
            self.assertEqual(local_bytes, (markdown.read_bytes(), event.read_bytes()))

            restore_root = root / "rebuilt output"
            restore_args = SimpleNamespace(config=None, sync_vps=True)
            output = io.StringIO()
            errors = io.StringIO()
            rebuilt: list[Path] = []

            def write_rebuilt(path: Path, _pack: object) -> None:
                Path(path).write_text("local rebuild", encoding="utf-8")
                rebuilt.append(Path(path))

            with (
                patch("agent_content.cli.load_config", return_value={"outputs_dir": str(restore_root)}),
                patch("agent_content.cli._refresh_codex_summaries", return_value=[SimpleNamespace(date=DATE)]),
                patch("agent_content.cli._discover_content_source_dates", return_value=[DATE]),
                patch("agent_content.cli._content_projects", return_value=[{}]),
                patch("agent_content.cli._build_pack_for_project", return_value=object()),
                patch("agent_content.cli._select_daily_pack", return_value=object()),
                patch("agent_content.cli.MarkdownWriter") as markdown_writer,
                patch("agent_content.cli.JsonWriter") as json_writer,
                patch("agent_content.cli.TodayPickWriter") as today_pick_writer,
                patch(
                    "agent_content.cli._write_topic_inbox_for_sync",
                    return_value=(result, [topic]),
                ),
                patch("agent_content.cli._sync_vps_if_requested", side_effect=failure),
                redirect_stdout(output),
                redirect_stderr(errors),
            ):
                markdown_writer.return_value.write.side_effect = write_rebuilt
                json_writer.return_value.write.side_effect = write_rebuilt
                today_pick_writer.return_value.write.side_effect = write_rebuilt
                self.assertEqual(1, cli.run_restore_history(restore_args))
            rendered = output.getvalue() + errors.getvalue()
            self.assertEqual(3, len(rebuilt))
            self.assertTrue(all(path.exists() for path in rebuilt))
            self.assertIn(reason, rendered)
            self.assertNotIn(str(root), rendered)
            self.assertNotIn(str(restore_root), rendered)
            self.assertNotIn("C:\\", rendered)
            self.assertNotIn(self.HOST, rendered)
            self.assertNotIn("internal-", rendered)
            self.assertNotIn("Traceback", rendered)
            self.assertEqual(local_bytes, (markdown.read_bytes(), event.read_bytes()))

    def test_sync_success_defers_path_output_until_transport_succeeds(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "Локальный путь УНИКАЛЬНЫЙ"
            result, topic, _ = _write_release_fixture(root)
            args = SimpleNamespace(
                config=None,
                sync_vps=True,
                date=DATE,
                vps_host=self.HOST,
                vps_path=self.VPS_PATH,
            )
            output = io.StringIO()

            def successful_transport(*_args: object) -> bool:
                print("TRANSPORT_SUCCESS_MARKER")
                return True

            with (
                patch("agent_content.cli.load_config", return_value={}),
                patch(
                    "agent_content.cli._refresh_codex_summaries",
                    return_value=[SimpleNamespace(date=DATE)],
                ),
                patch(
                    "agent_content.cli._write_topic_inbox_for_sync",
                    return_value=(result, [topic]),
                ),
                patch(
                    "agent_content.cli._sync_vps_if_requested",
                    side_effect=successful_transport,
                ),
                redirect_stdout(output),
            ):
                self.assertEqual(0, cli.run_export_nazai_inbox(args))

            rendered = output.getvalue()
            self.assertIn(str(result["inbox_dir"]), rendered)
            self.assertIn("Тематический inbox и OperatorEvent", rendered)
            self.assertLess(
                rendered.index("TRANSPORT_SUCCESS_MARKER"),
                rendered.index(str(result["inbox_dir"])),
            )

    def test_explicit_sync_early_returns_are_nonzero(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            restore_args = SimpleNamespace(config=None, sync_vps=True)
            with (
                patch("agent_content.cli.load_config", return_value={"outputs_dir": tmp}),
                patch("agent_content.cli._refresh_codex_summaries", return_value=[]),
                patch("agent_content.cli.ensure_dir", return_value=Path(tmp)),
                patch("agent_content.cli._discover_content_source_dates", return_value=[]),
            ):
                self.assertEqual(1, cli.run_restore_history(restore_args))

            day_args = SimpleNamespace(config=None, sync_vps=True, date=DATE)
            all_args = SimpleNamespace(config=None, sync_vps=True)
            with (
                patch("agent_content.cli.load_config", return_value={}),
                patch("agent_content.cli._refresh_codex_summaries", return_value=[]),
            ):
                self.assertEqual(1, cli.run_export_nazai_inbox(day_args))
                self.assertEqual(1, cli.run_export_nazai_inbox_all(all_args))


if __name__ == "__main__":
    unittest.main()
