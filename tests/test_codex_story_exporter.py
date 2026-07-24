from __future__ import annotations

import re
import unittest
from datetime import datetime
from types import SimpleNamespace

from agent_content.integrations.codex_story_exporter import CodexStoryExporter
from agent_content.integrations.codex_topic_exporter import CodexTopicExporter


def message(timestamp: str, role: str, text: str) -> SimpleNamespace:
    return SimpleNamespace(timestamp=timestamp, role=role, text=text)


def summary(
    session_id: str,
    messages: list[SimpleNamespace],
    *,
    project_name: str = "Naz-AI_Bot",
    archived: bool = True,
) -> SimpleNamespace:
    return SimpleNamespace(
        session_id=session_id,
        date="2026-07-21",
        messages=messages,
        project_name=project_name,
        project_path=rf"C:\Projects\{project_name}",
        archived=archived,
        dialog_title="Работа над публикацией",
        git_branch="main",
    )


class CodexStoryExporterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.topics = CodexTopicExporter(
            now=datetime.fromisoformat("2026-07-21T12:00:00+03:00")
        )
        self.stories = CodexStoryExporter()

    def test_completed_topic_becomes_grounded_story_not_transcript(self) -> None:
        source = summary(
            "story-complete",
            [
                message(
                    "2026-07-21T08:00:00+03:00",
                    "user",
                    "Добавь в Naz контур публикации в VK. Только dry-run; прод не трогать. "
                    "Это длинная исходная реплика, которая не должна целиком попасть в рассказ.",
                ),
                message(
                    "2026-07-21T08:01:00+03:00",
                    "codex",
                    "Добавлен `vk_queue.py`, внешних запросов не было.",
                ),
                message("2026-07-21T08:02:00+03:00", "user", "и очередь с README тоже"),
                message(
                    "2026-07-21T08:03:00+03:00",
                    "codex",
                    "4 теста прошли, README обновлён; публикации в VK не было.",
                ),
            ],
        )
        topic = self.topics.build_documents([source])[0]

        story = self.stories.build_documents([topic])[0]

        self.assertEqual(topic.relative_path, story.relative_path)
        self.assertEqual(topic.topic_id, story.topic_id)
        self.assertTrue(story.publishable)
        for fact in ("Naz", "VK", "dry-run", "vk_queue.py", "4 теста", "README", "публикации в VK не было"):
            self.assertIn(fact, story.text)
        for invented in ("Telegram", "5 тестов", "пост опубликован"):
            self.assertNotIn(invented, story.text)
        self.assertNotIn("## Диалог", story.text)
        self.assertNotIn("### Пользователь", story.text)
        self.assertNotIn("### Codex", story.text)
        self.assertNotIn("C:/Projects", story.text)
        self.assertNotIn(source.messages[0].text, story.text)
        self.assertRegex(story.text, r"Источник-хеш: sha256:[0-9a-f]{64}")
        self.assertIn("Формат: редакторский рассказ", story.text)

        body = story.text.split("## История", 1)[1]
        source_text = "\n".join(item.text for item in source.messages)
        for token in re.findall(r"\b\d+\b|\b[\w.-]+\.(?:py|md|json)\b", body):
            self.assertIn(token, source_text)

    def test_cancelled_and_open_topics_remain_present_and_fail_closed(self) -> None:
        cancelled = summary(
            "story-cancelled",
            [
                message("2026-07-21T08:00:00+03:00", "user", "Добавь автоматическую VK-публикацию"),
                message("2026-07-21T08:01:00+03:00", "codex", "Пока составил план, файлы не менялись"),
                message("2026-07-21T08:02:00+03:00", "user", "давай откатим"),
                message("2026-07-21T08:03:00+03:00", "codex", "Откатывать нечего, правок не было"),
            ],
        )
        opened = summary(
            "story-open",
            [
                message("2026-07-21T11:00:00+03:00", "user", "Исправь повторную отправку"),
                message("2026-07-21T11:01:00+03:00", "codex", "Начал диагностику"),
            ],
            archived=False,
        )

        stories = self.stories.build_documents(self.topics.build_documents([cancelled, opened]))
        by_session = {item.session_id: item for item in stories}

        cancelled_story = by_session["story-cancelled"]
        self.assertFalse(cancelled_story.publishable)
        self.assertIn("Результат: отменено пользователем", cancelled_story.text)
        self.assertIn("Завершённый результат для публикации не заявляется", cancelled_story.text)
        self.assertNotIn("функция добавлена", cancelled_story.text.casefold())

        open_story = by_session["story-open"]
        self.assertFalse(open_story.publishable)
        self.assertIn("Статус: открыта", open_story.text)
        self.assertIn("Итоговый результат пока не подтверждён", open_story.text)
        self.assertNotIn("исправлено", open_story.text.casefold())

    def test_coverage_identity_determinism_and_no_cross_topic_bleed(self) -> None:
        vk = summary(
            "story-shared-session",
            [
                message("2026-07-21T08:00:00+03:00", "user", "Добавь VK_ONLY_CANARY публикацию"),
                message("2026-07-21T08:01:00+03:00", "codex", "VK_ONLY_CANARY реализован и проверен"),
                message("2026-07-21T08:02:00+03:00", "user", "Создай TELEGRAM_ONLY_CANARY очередь"),
                message("2026-07-21T08:03:00+03:00", "codex", "TELEGRAM_ONLY_CANARY создан и протестирован"),
            ],
        )
        raw_topics = self.topics.build_documents([vk])

        first = self.stories.build_documents(raw_topics)
        second = self.stories.build_documents(reversed(raw_topics))

        self.assertEqual(first, second)
        self.assertEqual(
            {(item.relative_path, item.topic_id, item.project_name, item.date, item.session_id) for item in raw_topics},
            {(item.relative_path, item.topic_id, item.project_name, item.date, item.session_id) for item in first},
        )
        self.assertEqual(2, len(first))
        vk_story = next(item for item in first if "VK_ONLY_CANARY" in item.text)
        telegram_story = next(item for item in first if "TELEGRAM_ONLY_CANARY" in item.text)
        self.assertNotIn("TELEGRAM_ONLY_CANARY", vk_story.text)
        self.assertNotIn("VK_ONLY_CANARY", telegram_story.text)

    def test_closed_topic_without_confirmed_result_is_downgraded(self) -> None:
        source = summary(
            "story-no-result",
            [
                message("2026-07-21T08:00:00+03:00", "user", "Добавь новую очередь"),
                message("2026-07-21T08:01:00+03:00", "codex", "Сейчас смотрю файлы"),
                message("2026-07-21T08:02:00+03:00", "codex", "Хорошо, жду обновлённое задание"),
            ],
        )
        topic = self.topics.build_documents([source])[0]
        self.assertTrue(topic.publishable)

        story = self.stories.build_documents([topic])[0]

        self.assertFalse(story.publishable)
        self.assertIn("Результат: результат не зафиксирован", story.text)
        self.assertIn("Автопубликация: запрещена", story.text)

    def test_future_plans_are_not_results_and_weak_chatter_is_not_exported(self) -> None:
        planned = summary(
            "story-plan",
            [
                message("2026-07-21T08:00:00+03:00", "user", "Исправь загрузку отчёта"),
                message("2026-07-21T08:01:00+03:00", "codex", "Принял задачу: исправлю парсер и запущу тесты"),
                message("2026-07-21T08:02:00+03:00", "codex", "Сейчас проверю конфигурацию"),
            ],
        )
        weak = summary(
            "story-weak",
            [
                message("2026-07-21T09:00:00+03:00", "user", "всё норм?"),
                message("2026-07-21T09:01:00+03:00", "codex", "Да, всё норм"),
            ],
        )

        planned_story = self.stories.build_documents(self.topics.build_documents([planned]))[0]
        weak_stories = self.stories.build_documents(self.topics.build_documents([weak]))

        self.assertFalse(planned_story.publishable)
        self.assertIn("Результат: результат не зафиксирован", planned_story.text)
        self.assertNotIn("исправлю парсер", planned_story.text.casefold())
        self.assertEqual([], weak_stories)

    def test_scope_lock_and_operational_secrets_stay_out_of_story(self) -> None:
        source = summary(
            "story-scope",
            [
                message(
                    "2026-07-21T08:00:00+03:00",
                    "user",
                    "Выполни только PR #22: добавь защиту публикации и проверь итог.",
                ),
                message(
                    "2026-07-21T08:01:00+03:00",
                    "codex",
                    "PR #22 реализован и проверен. Путь /opt/private/app, fingerprint SHA256:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA.",
                ),
                message(
                    "2026-07-21T08:02:00+03:00",
                    "codex",
                    "PR #23 подготовлен и перенесён.",
                ),
            ],
        )

        story = self.stories.build_documents(self.topics.build_documents([source]))[0]

        self.assertTrue(story.publishable)
        self.assertIn("PR #22", story.text)
        self.assertNotIn("PR #23", story.text)
        self.assertNotIn("/opt/", story.text)
        self.assertNotIn("SHA256:AAAA", story.text)
        self.assertRegex(story.text, r"Источник-хеш: sha256:[0-9a-f]{64}")


if __name__ == "__main__":
    unittest.main()
