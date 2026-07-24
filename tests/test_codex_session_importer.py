from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_content.cli import _content_projects, _select_daily_pack, build_live_payload
from agent_content.integrations.codex_session_importer import CodexMessage, CodexSessionImporter


def write_rollout(path: Path, meta: dict, events: list[tuple[str, str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    records = [
        {
            "timestamp": meta.get("timestamp", "2026-07-21T20:00:00Z"),
            "type": "session_meta",
            "payload": meta,
        },
        {
            "timestamp": "2026-07-21T20:00:01Z",
            "type": "world_state",
            "payload": {"large_ignored_record": "x" * 100_000},
        },
    ]
    for timestamp, event_type, message in events:
        records.append(
            {
                "timestamp": timestamp,
                "type": "event_msg",
                "payload": {"type": event_type, "message": message},
            }
        )
    path.write_text(
        "\n".join(json.dumps(item, ensure_ascii=False) for item in records) + "\n",
        encoding="utf-8",
    )


def tree_hash(root: Path) -> dict[str, str]:
    return {
        str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


class CodexSessionImporterTests(unittest.TestCase):
    def test_exact_user_retry_series_ends_only_after_codex_answer(self) -> None:
        importer = CodexSessionImporter(Path("unused"))
        messages = [
            CodexMessage("2026-07-21T10:00:00Z", "user", "A"),
            CodexMessage("2026-07-21T10:00:03Z", "user", "A"),
            CodexMessage("2026-07-21T10:02:18Z", "user", "A"),
            CodexMessage("2026-07-21T10:02:19Z", "user", "B"),
            CodexMessage("2026-07-21T10:02:20Z", "user", "A"),
            CodexMessage("2026-07-21T10:02:21Z", "codex", "Ответ"),
            CodexMessage("2026-07-21T10:03:00Z", "user", "A"),
        ]

        compressed = importer._compress_messages(messages)

        self.assertEqual(
            [("user", "A"), ("user", "B"), ("user", "A"), ("codex", "Ответ"), ("user", "A")],
            [(item.role, item.text) for item in compressed],
        )

    def test_session_title_names_transcript_and_transport_duplicates_are_collapsed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            sessions = root / "sessions"
            session_id = "aaaaaaaa-1111-2222-3333-bbbbbbbbbbbb"
            write_rollout(
                sessions / "main.jsonl",
                {
                    "id": session_id,
                    "session_id": session_id,
                    "cwd": r"C:\Projects\Naz-AI_Bot",
                    "source": "vscode",
                    "thread_source": "user",
                },
                [
                    ("2026-07-21T10:00:00.000Z", "user_message", "same transport request"),
                    ("2026-07-21T10:00:00.015Z", "user_message", "same transport request"),
                    ("2026-07-21T10:01:00.000Z", "user_message", "same transport request"),
                    (
                        "2026-07-21T10:02:00.000Z",
                        "agent_message",
                        "done on deploy@192.0.2.10 at /opt/private/app with "
                        "SHA256:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA and "
                        "hf_abcdefghijklmnopqrstuvwxyz123456",
                    ),
                    ("2026-07-21T10:03:00.000Z", "user_message", "same transport request"),
                    ("2026-07-21T10:04:00.000Z", "agent_message", "done again"),
                ],
            )
            session_index = root / "session_index.jsonl"
            session_index.write_text(
                json.dumps(
                    {"id": session_id, "thread_name": "Добавить контур публикации в VK"},
                    ensure_ascii=False,
                )
                + "\n",
                encoding="utf-8",
            )

            output = root / "out"
            importer = CodexSessionImporter(sessions, session_index_path=session_index)
            written = importer.import_sessions([], output, clear=True)

            self.assertEqual(1, len(written))
            self.assertIn("добавить-контур-публикации-в-vk", written[0].name.casefold())
            text = written[0].read_text(encoding="utf-8")
            self.assertIn("Тема диалога: Добавить контур публикации в VK", text)
            self.assertEqual(2, text.count("same transport request"))
            self.assertNotIn("192.0.2.10", text)
            self.assertIn("[SSH-адрес скрыт]", text)
            self.assertNotIn("hf_abcdefghijklmnopqrstuvwxyz123456", text)
            self.assertIn("[токен скрыт]", text)
            self.assertNotIn("/opt/private/app", text)
            self.assertIn("[серверный путь скрыт]", text)
            self.assertNotIn("SHA256:AAAA", text)
            self.assertIn("[SSH-отпечаток скрыт]", text)

    def test_dynamic_projects_and_live_payload_ignore_empty_fallbacks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            central = root / "ai-logs"
            known = central / "Known"
            discovered = central / "New project"
            known.mkdir(parents=True)
            discovered.mkdir()
            config = {
                "projects": [
                    {
                        "name": "Known",
                        "path": str(root / "known-repo"),
                        "notes_dir": str(root / "known-notes"),
                        "ai_logs_dir": str(known),
                        "terminal_logs_dir": str(root / "known-terminal"),
                    }
                ],
                "ai_logs_dir": str(central),
                "notes_dir": str(root / "notes"),
                "terminal_logs_dir": str(root / "terminal"),
            }
            self.assertEqual({"Known", "New project"}, {item["name"] for item in _content_projects(config)})

        projects = [
            {"name": "empty", "repo_path": "empty"},
            {"name": "real", "repo_path": "real"},
        ]

        def result_for(project: dict, _config: dict, _date: str) -> dict:
            source = "system:no_codex_chat" if project["name"] == "empty" else "ai_logs:chat.md"
            return {
                "project": project,
                "pack": {
                    "raw_context": {"terminal_logs": []},
                    "events": [{"source": source}],
                    "hooks": [],
                    "best_format": {},
                    "do_not_publish": [],
                },
            }

        with patch("agent_content.cli._content_projects", return_value=projects), patch(
            "agent_content.cli._build_pack_for_project", side_effect=result_for
        ):
            payload = build_live_payload({}, "2026-07-14")
        self.assertEqual(["real"], [item["name"] for item in payload["projects"]])

    def test_fallback_events_cannot_become_a_content_pack(self) -> None:
        fallback = {
            "pack": {
                "events": [
                    {
                        "source": "system:no_codex_chat",
                        "title": "No Codex work chat found",
                    }
                ]
            }
        }
        self.assertIsNone(_select_daily_pack("2026-07-14", [fallback]))

    def test_full_session_identity_prevents_filename_collisions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            sessions = root / "sessions"
            common = "12345678-123"
            for suffix in ("a", "b"):
                session_id = common + suffix + "-0000-0000-000000000000"
                write_rollout(
                    sessions / f"{suffix}.jsonl",
                    {
                        "id": session_id,
                        "session_id": session_id,
                        "cwd": r"C:\Projects\Same",
                        "source": "vscode",
                        "thread_source": "user",
                    },
                    [("2026-07-21T10:00:00Z", "user_message", f"message-{suffix}")],
                )

            output = root / "out"
            written = CodexSessionImporter(sessions).import_sessions([], output, clear=True)
            self.assertEqual(2, len(written))
            self.assertEqual(2, len({path.name for path in written}))

    def test_full_user_history_is_split_by_local_day_and_subagents_are_excluded(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            active = root / "sessions"
            archived = root / "archived_sessions"
            output = root / "ai-logs"
            output.mkdir()
            (output / "stale.json").write_text("{}", encoding="utf-8")

            main_id = "11111111-2222-3333-4444-555555555555"
            day_two_events = [
                ("2026-07-21T21:00:00Z", "agent_message", "L" * 1200 + "\n```python\nprint('kept')\n```"),
            ]
            day_two_events.extend(
                (f"2026-07-21T21:{minute:02d}:10Z", "agent_message", f"reply-{index}")
                for index, minute in enumerate(range(1, 60), start=1)
            )
            day_two_events.extend(
                [
                    ("2026-07-21T22:10:00Z", "user_message", "same text"),
                    ("2026-07-21T22:11:00Z", "user_message", "same text"),
                    ("2026-07-21T22:12:00Z", "agent_message", "the 61st tail is present"),
                ]
            )
            write_rollout(
                active / "main.jsonl",
                {
                    "id": main_id,
                    "session_id": main_id,
                    "cwd": r"C:\Projects\Новый проект",
                    "source": "vscode",
                    "thread_source": "user",
                },
                [
                    (
                        "2026-07-21T20:59:59Z",
                        "user_message",
                        "path C:\\Projects\\Private\\file.py\nimportant next line",
                    ),
                    *day_two_events,
                ],
            )
            write_rollout(
                active / "subagent.jsonl",
                {
                    "id": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
                    "session_id": main_id,
                    "cwd": r"C:\Projects\Новый проект",
                    "source": {"subagent": "guardian"},
                    "thread_source": "subagent",
                    "parent_thread_id": main_id,
                },
                [("2026-07-21T20:30:00Z", "agent_message", "guardian garbage must not appear")],
            )
            archive_id = "99999999-8888-7777-6666-555555555555"
            write_rollout(
                archived / "archived.jsonl",
                {
                    "id": archive_id,
                    "session_id": archive_id,
                    "cwd": r"C:\Elsewhere\Archive",
                    "source": "vscode",
                    "thread_source": "user",
                },
                [("2026-07-20T10:00:00Z", "user_message", "archived visible chat")],
            )

            importer = CodexSessionImporter([active, archived], timezone="Europe/Moscow")
            written = importer.import_sessions([], output, clear=True, format="detailed")

            self.assertEqual(3, len(written))
            self.assertTrue(all(path.suffix == ".md" for path in output.rglob("*") if path.is_file()))
            self.assertFalse((output / "stale.json").exists())
            self.assertTrue(any(path.name.startswith("2026-07-21-codex-") for path in written))
            self.assertTrue(any(path.name.startswith("2026-07-22-codex-") for path in written))
            self.assertTrue(any(path.name.startswith("2026-07-20-codex-") for path in written))
            self.assertTrue(any("Новый-проект" in str(path.parent) for path in written))

            combined = "\n".join(path.read_text(encoding="utf-8") for path in written)
            self.assertIn("important next line", combined)
            self.assertIn("print('kept')", combined)
            self.assertIn("the 61st tail is present", combined)
            self.assertIn("L" * 1200, combined)
            self.assertEqual(1, combined.count("same text"))
            self.assertIn("archived visible chat", combined)
            self.assertNotIn("guardian garbage", combined)
            self.assertNotIn(r"C:\Projects\Private", combined)

            first_hashes = tree_hash(output)
            importer.import_sessions([], output, clear=True, format="detailed")
            self.assertEqual(first_hashes, tree_hash(output))


if __name__ == "__main__":
    unittest.main()
