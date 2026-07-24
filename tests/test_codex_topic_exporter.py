from __future__ import annotations

import re
import unittest
from datetime import datetime
from types import SimpleNamespace

from agent_content.integrations.codex_topic_exporter import CodexTopicExporter


def message(timestamp: str, role: str, text: str) -> SimpleNamespace:
    return SimpleNamespace(timestamp=timestamp, role=role, text=text)


def summary(
    session_id: str,
    date: str,
    messages: list[SimpleNamespace],
    *,
    project_name: str = "Naz-AI_Bot",
    archived: bool = False,
    dialog_title: str = "Работа над Naz",
    git_branch: str = "main",
) -> SimpleNamespace:
    return SimpleNamespace(
        session_id=session_id,
        date=date,
        messages=messages,
        project_name=project_name,
        project_path=r"C:\Projects\Naz-AI_Bot",
        archived=archived,
        dialog_title=dialog_title,
        git_branch=git_branch,
    )


class CodexTopicExporterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.exporter = CodexTopicExporter(
            now=datetime.fromisoformat("2026-07-21T14:30:00+03:00")
        )

    def test_partitions_messages_once_and_builds_project_date_topic_paths(self) -> None:
        first_request = (
            "Добавь контур публикации в VK с безопасным dry-run режимом и проверкой "
            + "условий перед отправкой " * 7
            + " BODY_M1"
        )
        second_request = (
            "Проведи диагностику публикации в VK и проверь фактическую очередь "
            + "без изменения внешнего состояния " * 6
            + " BODY_M5"
        )
        item = summary(
            "session-topic-1",
            "2026-07-21",
            [
                message("2026-07-21T09:00:00+03:00", "user", first_request),
                message("2026-07-21T09:01:00+03:00", "codex", "Ответ по первой задаче BODY_M2"),
                message("2026-07-21T09:05:00+03:00", "user", "и проверку тоже BODY_M3"),
                message("2026-07-21T09:06:00+03:00", "codex", "Проверка добавлена BODY_M4"),
                message("2026-07-21T11:00:00+03:00", "user", second_request),
                message("2026-07-21T11:02:00+03:00", "codex", "Диагностика завершена BODY_M6"),
            ],
        )

        documents = self.exporter.build_documents([item])

        self.assertEqual(2, len(documents))
        self.assertTrue(all(document.publishable for document in documents))
        self.assertEqual("Naz-AI_Bot", documents[0].project_name)
        for document in documents:
            parts = document.relative_path.parts
            self.assertEqual("Naz-AI_Bot", parts[0])
            self.assertEqual("2026-07-21", parts[1])
            self.assertRegex(
                parts[2],
                r"^2026-07-21-\d{4}--.+--t-[0-9a-f]{12}\.md$",
            )
            self.assertRegex(document.topic_id, r"^[0-9a-f]{12}$")

        rendered_dialogue = "\n".join(
            document.text.split("## Диалог", 1)[1] for document in documents
        )
        for marker in ("BODY_M1", "BODY_M2", "BODY_M3", "BODY_M4", "BODY_M5", "BODY_M6"):
            self.assertEqual(1, rendered_dialogue.count(marker), marker)
        self.assertIn("Добавление: контур публикации в VK", documents[0].title)
        self.assertIn("Проведи диагностику публикации в VK", documents[1].title)

    def test_substantive_user_burst_before_codex_stays_in_one_episode(self) -> None:
        long_one = "Создай новый обработчик публикации. " + "Подробность " * 20
        long_two = "Добавь к нему проверку очереди. " + "Уточнение " * 20
        next_task = "Проверь итоговый обработчик публикации после изменений."
        item = summary(
            "session-topic-2",
            "2026-07-21",
            [
                message("2026-07-21T10:00:00+03:00", "user", long_one),
                message("2026-07-21T10:00:10+03:00", "user", long_two),
                message("2026-07-21T10:02:00+03:00", "codex", "Сделано"),
                message("2026-07-21T10:03:00+03:00", "user", next_task),
                message("2026-07-21T10:04:00+03:00", "codex", "Проверено"),
            ],
            archived=True,
        )

        documents = self.exporter.build_documents([item])

        self.assertEqual(2, len(documents))
        first_dialogue = documents[0].text.split("## Диалог", 1)[1]
        self.assertIn(long_one, first_dialogue)
        self.assertIn(long_two, first_dialogue)
        self.assertNotIn(next_task, first_dialogue)

    def test_open_or_unanchored_episode_is_not_publishable(self) -> None:
        open_item = summary(
            "session-open",
            "2026-07-21",
            [
                message("2026-07-21T14:00:00+03:00", "user", "Добавь публикацию в VK"),
                message("2026-07-21T14:05:00+03:00", "codex", "Начал работу"),
            ],
        )
        continuation = summary(
            "session-continuation",
            "2026-07-20",
            [
                message("2026-07-20T09:00:00+03:00", "user", "готово"),
                message("2026-07-20T09:01:00+03:00", "codex", "Да"),
            ],
            archived=True,
        )

        documents = self.exporter.build_documents([open_item, continuation])
        by_session = {document.session_id: document for document in documents}

        self.assertFalse(by_session["session-open"].publishable)
        self.assertIn("Статус: открыта", by_session["session-open"].text)
        self.assertFalse(by_session["session-continuation"].publishable)
        self.assertIn("Статус: закрыта", by_session["session-continuation"].text)

    def test_user_cancellation_stays_with_topic_and_blocks_publication(self) -> None:
        item = summary(
            "session-cancelled",
            "2026-07-09",
            [
                message("2026-07-09T10:00:00+03:00", "user", "Добавь VK browser-публикацию"),
                message("2026-07-09T10:02:00+03:00", "codex", "Проверяю контур"),
                message("2026-07-09T10:03:00+03:00", "user", "давай откатим, это не подходит"),
                message("2026-07-09T10:04:00+03:00", "codex", "Откатывать нечего, правок не было"),
            ],
            archived=True,
        )

        documents = self.exporter.build_documents([item])

        self.assertEqual(1, len(documents))
        self.assertFalse(documents[0].publishable)
        self.assertIn("Результат: отменено пользователем", documents[0].text)
        self.assertIn("давай откатим", documents[0].text)

    def test_idle_gap_does_not_promote_weak_replies_but_keeps_short_actionable_request(self) -> None:
        item = summary(
            "session-idle",
            "2026-07-21",
            [
                message("2026-07-21T08:00:00+03:00", "user", "Добавь безопасную очередь публикации"),
                message("2026-07-21T08:01:00+03:00", "codex", "Очередь готова"),
                message("2026-07-21T10:00:00+03:00", "user", "!"),
                message("2026-07-21T10:01:00+03:00", "codex", "Продолжаю"),
                message("2026-07-21T12:00:00+03:00", "user", "1"),
                message("2026-07-21T12:01:00+03:00", "codex", "Принято"),
                message("2026-07-21T14:00:00+03:00", "user", "дважды, надо исправить"),
                message("2026-07-21T14:01:00+03:00", "codex", "Повтор исправлен"),
            ],
            archived=True,
        )

        documents = self.exporter.build_documents([item])

        self.assertEqual(2, len(documents))
        self.assertIn("\n!\n", documents[0].text)
        self.assertIn("\n1\n", documents[0].text)
        self.assertNotIn("--1--", documents[0].relative_path.name)
        self.assertEqual("дважды, надо исправить", documents[1].title)
        self.assertIn("Граница: idle_gap", documents[1].text)
        self.assertTrue(documents[1].publishable)

    def test_generic_goal_heading_is_skipped_for_literal_topic_title(self) -> None:
        item = summary(
            "session-goal-heading",
            "2026-07-21",
            [
                message(
                    "2026-07-21T08:00:00+03:00",
                    "user",
                    "Goal\nDiagnose and minimally fix the OpenRouter 403 failure handling.\n\nContext\n"
                    + "Detailed constraints. " * 12,
                ),
                message("2026-07-21T08:01:00+03:00", "codex", "Failure handling fixed"),
            ],
            archived=True,
        )

        document = self.exporter.build_documents([item])[0]

        self.assertTrue(document.title.startswith("Diagnose and minimally fix"))
        self.assertNotEqual("Goal", document.title)
        self.assertNotIn("--goal--", document.relative_path.name)

    def test_short_tell_me_request_after_idle_is_a_concrete_topic(self) -> None:
        item = summary(
            "session-short-question",
            "2026-07-21",
            [
                message("2026-07-21T08:00:00+03:00", "user", "Добавь режим приватного доступа"),
                message("2026-07-21T08:01:00+03:00", "codex", "Режим добавлен"),
                message("2026-07-21T10:00:00+03:00", "user", "подскажи где поменять на публичный"),
                message("2026-07-21T10:01:00+03:00", "codex", "Показываю нужную настройку"),
            ],
            archived=True,
        )

        documents = self.exporter.build_documents([item])

        self.assertEqual(2, len(documents))
        self.assertEqual("подскажи где поменять на публичный", documents[1].title)
        self.assertIn("Граница: idle_gap", documents[1].text)
        self.assertTrue(documents[1].publishable)

    def test_constraints_and_rollback_tasks_are_not_cancellations_but_stop_is(self) -> None:
        first = summary(
            "session-cancel-matrix",
            "2026-07-21",
            [
                message("2026-07-21T08:00:00+03:00", "user", "Добавь защиту релиза"),
                message("2026-07-21T08:01:00+03:00", "codex", "Защита добавлена"),
                message(
                    "2026-07-21T08:02:00+03:00",
                    "user",
                    "Не делай force push, не делай deploy, гейт не надо ослаблять. Текст кнопки: «Не надо сохранять».",
                ),
                message("2026-07-21T08:03:00+03:00", "codex", "Ограничения сохранены"),
                message("2026-07-21T08:03:10+03:00", "user", "стоп. мы можем проверить из локалки?"),
                message("2026-07-21T08:03:20+03:00", "codex", "Да, проверяем локально"),
                message("2026-07-21T08:04:00+03:00", "user", "Откати merge-коммиты безопасно"),
                message("2026-07-21T08:05:00+03:00", "codex", "Коммиты откачены"),
                message("2026-07-21T08:06:00+03:00", "user", "так все, стоп"),
            ],
            archived=True,
        )
        standalone = summary(
            "session-standalone-stop",
            "2026-07-21",
            [message("2026-07-21T09:00:00+03:00", "user", "стоп")],
            archived=True,
        )
        undo_all = summary(
            "session-undo-all",
            "2026-07-21",
            [message("2026-07-21T09:10:00+03:00", "user", "отмени все изменения")],
            archived=True,
        )

        documents = self.exporter.build_documents([first, standalone, undo_all])
        by_session = {}
        for document in documents:
            by_session.setdefault(document.session_id, []).append(document)

        work = by_session["session-cancel-matrix"]
        self.assertEqual(2, len(work))
        self.assertTrue(work[0].publishable)
        self.assertIn("Результат: без явной отмены", work[0].text)
        self.assertFalse(work[1].publishable)
        self.assertIn("Откати merge-коммиты", work[1].text)
        self.assertIn("Результат: отменено пользователем", work[1].text)
        self.assertFalse(by_session["session-standalone-stop"][0].publishable)
        self.assertIn("Результат: отменено пользователем", by_session["session-standalone-stop"][0].text)
        self.assertFalse(by_session["session-undo-all"][0].publishable)
        self.assertIn("Результат: отменено пользователем", by_session["session-undo-all"][0].text)

    def test_output_is_deterministic_and_uses_moscow_local_date(self) -> None:
        item = summary(
            "session-midnight",
            "2026-07-21",
            [
                message("2026-07-20T21:30:00Z", "user", "Проведи проверку ночной публикации"),
                message("2026-07-20T21:31:00Z", "codex", "Проверка выполнена"),
            ],
            archived=True,
        )

        first = self.exporter.build_documents([item])
        second = self.exporter.build_documents([item])

        self.assertEqual(first, second)
        self.assertEqual("2026-07-21", first[0].date)
        self.assertTrue(
            re.match(
                r"^2026-07-21-0030--.+--t-[0-9a-f]{12}\.md$",
                first[0].relative_path.name,
            )
        )

    def test_same_session_is_partitioned_at_local_day_boundary(self) -> None:
        first_day = summary(
            "session-two-days",
            "2026-07-20",
            [
                message("2026-07-20T20:50:00Z", "user", "Проведи вечернюю проверку"),
                message("2026-07-20T20:55:00Z", "codex", "Вечерняя проверка начата"),
            ],
        )
        second_day = summary(
            "session-two-days",
            "2026-07-21",
            [
                message("2026-07-20T21:05:00Z", "codex", "Ночной результат сохранён"),
            ],
        )

        documents = self.exporter.build_documents([first_day, second_day])

        self.assertEqual(["2026-07-20", "2026-07-21"], [item.date for item in documents])
        self.assertIn("Вечерняя проверка начата", documents[0].text)
        self.assertNotIn("Ночной результат сохранён", documents[0].text)
        self.assertIn("Ночной результат сохранён", documents[1].text)
        self.assertFalse(documents[1].publishable)

    def test_greeting_does_not_swallow_following_question_or_post_cancel_task(self) -> None:
        item = summary(
            "session-greeting-boundary",
            "2026-07-21",
            [
                message("2026-07-21T08:00:00+03:00", "user", "привет"),
                message("2026-07-21T08:00:10+03:00", "codex", "Привет"),
                message("2026-07-21T08:01:00+03:00", "user", "как включить агента для сегодняшней сессии"),
                message("2026-07-21T08:02:00+03:00", "codex", "Настройка добавлена"),
                message("2026-07-21T08:03:00+03:00", "user", "так всё, стоп"),
                message("2026-07-21T08:04:00+03:00", "codex", "Остановлено"),
                message("2026-07-21T08:05:00+03:00", "user", "обнови адрес отчётов для новой группы"),
                message("2026-07-21T08:06:00+03:00", "codex", "Адрес обновлён"),
            ],
            archived=True,
        )

        documents = self.exporter.build_documents([item])

        self.assertEqual(3, len(documents))
        self.assertFalse(documents[0].publishable)
        self.assertIn("как включить агента", documents[1].title)
        self.assertTrue(documents[1].cancelled)
        self.assertFalse(documents[2].cancelled)
        self.assertEqual("post_cancel_request", documents[2].boundary_reason)
        self.assertIn("Обновление: адрес отчётов", documents[2].title)

    def test_long_title_is_editorialized_without_dangling_service_word(self) -> None:
        item = summary(
            "session-title",
            "2026-07-21",
            [
                message(
                    "2026-07-21T08:00:00+03:00",
                    "user",
                    "Добавь контур публикации в VK с проверкой очереди и безопасным режимом, "
                    "чтобы затем подготовить письмо редактору и продолжить работу в другом чате",
                ),
                message("2026-07-21T08:01:00+03:00", "codex", "Контур добавлен"),
            ],
            archived=True,
        )

        title = self.exporter.build_documents([item])[0].title

        self.assertLessEqual(len(title), self.exporter.TITLE_CHARS + 1)
        self.assertTrue(title.startswith("Добавление:"))
        self.assertNotRegex(title.casefold(), r"\b(?:в|как|чтобы|мне|нужен)$")


if __name__ == "__main__":
    unittest.main()
