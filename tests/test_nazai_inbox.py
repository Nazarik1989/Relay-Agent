from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from agent_content.integrations.nazai_inbox import NazAiInbox


class NazAiInboxTests(unittest.TestCase):
    def test_topic_tree_is_atomic_text_only_and_rejects_unsafe_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            inbox = NazAiInbox(local_path=str(root / "naz"), inbox_dir="content_inbox/agent_content")
            result = inbox.write_documents(
                {
                    Path("Naz-AI_Bot/2026-07-21/topic-one.md"): "first topic",
                    Path("Void-entity/2026-07-22/topic-two.md"): "second topic",
                }
            )

            target = Path(result["inbox_dir"])
            self.assertEqual(2, result["project_count"])
            self.assertEqual(2, result["date_count"])
            self.assertEqual(2, result["document_count"])
            self.assertTrue((target / "Naz-AI_Bot/2026-07-21/topic-one.md").is_file())
            self.assertTrue((target / "Void-entity/2026-07-22/topic-two.md").is_file())

            before = sorted(path.relative_to(target) for path in target.rglob("*.md"))
            with self.assertRaises(ValueError):
                inbox.write_documents({Path("../escape.md"): "unsafe"})
            self.assertEqual(before, sorted(path.relative_to(target) for path in target.rglob("*.md")))

            inbox.write_documents({Path("Naz-AI_Bot/2026-07-22/new-topic.md"): "replacement"})
            self.assertEqual(
                [Path("Naz-AI_Bot/2026-07-22/new-topic.md")],
                [path.relative_to(target) for path in target.rglob("*.md")],
            )

    def test_archive_is_atomic_text_only_and_has_no_sidecars(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            source.mkdir()
            first = source / "2026-07-21-codex-first.md"
            second = source / "2026-07-21-codex-second.md"
            ignored = source / "2026-07-21-content-pack.json"
            first.write_text("first chat", encoding="utf-8")
            second.write_text("second chat", encoding="utf-8")
            ignored.write_text("{}", encoding="utf-8")

            inbox = NazAiInbox(local_path=str(root / "naz"), inbox_dir="content_inbox/agent_content")
            result = inbox.write_archive({"2026-07-21": [first, ignored], "2026-07-22": [second]})

            target = Path(result["inbox_dir"])
            files = sorted(path for path in target.rglob("*") if path.is_file())
            self.assertEqual(2, len(files))
            self.assertTrue(all(path.suffix == ".md" for path in files))
            self.assertFalse(any(path.name in {"manifest.json", "README.md"} for path in files))

            inbox.write_archive({"2026-07-22": [second]})
            self.assertFalse((target / "2026-07-21").exists())
            self.assertEqual([second.name], [path.name for path in target.rglob("*.md")])


if __name__ == "__main__":
    unittest.main()
