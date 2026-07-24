from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from agent_content.analyzers.privacy_scanner import PrivacyScanner
from agent_content.integrations.codex_topic_exporter import TopicDocument, TopicSourceMessage


@dataclass(frozen=True)
class StoryDocument:
    relative_path: Path
    text: str
    project_name: str
    date: str
    session_id: str
    topic_id: str
    title: str
    publishable: bool
    source_hash: str


class CodexStoryExporter:
    """Turn a topic transcript into a grounded, readable editorial story card.

    The full role-by-role transcript remains in ``ai-logs``.  This renderer is
    deliberately extractive: every concrete claim, number and identifier in
    the story comes from the topic itself.  It does not call an LLM and cannot
    upgrade a topic's publication permission.
    """

    FORMAT_VERSION = "editorial-story-v1"
    EXCERPT_LIMIT = 360

    _GENERIC_LINE_RE = re.compile(
        r"^(?:goal|context|scope|request|task|objective|verification|do not|"
        r"цель|контекст|задача|требования|ограничения|проверка|что нужно|итог|результат)\s*:?[\s#]*$",
        re.IGNORECASE,
    )
    _METADATA_LINE_RE = re.compile(
        r"^(?:проект|github|gitlab|репозиторий|рабоч(?:ий|ая)\s+(?:каталог|папка)|"
        r"локальн(?:ый|ая)\s+(?:путь|папка))\s*:",
        re.IGNORECASE,
    )
    _WEAK_RE = re.compile(
        r"^(?:[!?.,…\d]+|го|goal|ок(?:ей)?|привет|ку\s*ку|готово|вс[её]\s+норм\??)$",
        re.IGNORECASE,
    )
    _SECTION_LABEL_RE = re.compile(
        r"^(?:что\s+(?:сделано|изменил(?:ось)?|получилось|проверил|проверено|будем\s+делать)|"
        r"сделано|готово|проверил|проверено|изменения|проверка|результат|итог|план|дальше)\s*:$",
        re.IGNORECASE,
    )
    _CONSTRAINT_RE = re.compile(
        r"\b(?:без|только|нельзя|важно|обязательно|необходимо|долж(?:ен|на|но|ны)|"
        r"не\s+(?:делай|делать|трогай|трогать|публикуй|публиковать|отправляй|отправлять|"
        r"меняй|менять|удаляй|удалять|запускай|запускать|ослабляй|ослаблять)|"
        r"dry[- ]?run|внешн\w*\s+(?:запрос|публикац)\w*\s+не\s+был)\b",
        re.IGNORECASE,
    )
    _PIVOT_RE = re.compile(
        r"\b(?:но|однако|ошибка|баг|не\s+работает|сломал|дважды|повтор|"
        r"исправ|проверь|уточн|надо|нужно|подскажи|вместо|теперь)\w*\b",
        re.IGNORECASE,
    )
    _RESULT_RE = re.compile(
        r"\b(?:готово|сделано|"
        r"исправ(?:ил(?:а|и)?|лен(?:а|о|ы)?)|"
        r"добав(?:ил(?:а|и)?|лен(?:а|о|ы)?)|"
        r"реализ(?:овал(?:а|и)?|ован(?:а|о|ы)?)|"
        r"обнов(?:ил(?:а|и)?|л[её]н(?:а|о|ы)?)|"
        r"провер(?:ил(?:а|и)?|ен(?:а|о|ы)?)|"
        r"наш(?:[её]л|ли)|причина\s+(?:найдена|оказалась)|"
        r"тест\w*\s+(?:прош\w*|зел[её]н\w*)|"
        r"создан(?:а|о|ы)?|удал[её]н(?:а|о|ы)?|сохран[её]н(?:а|о|ы)?|"
        r"настроен(?:а|о|ы)?|подготовлен(?:а|о|ы)?|зафиксирован(?:а|о|ы)?|"
        r"раздел[её]н(?:а|о|ы)?|перенес[её]н(?:а|о|ы)?|"
        r"done|completed|fixed|added|implemented|updated|verified|passed|created|removed|saved|configured|prepared|moved)\b",
        re.IGNORECASE,
    )
    _IN_PROGRESS_RE = re.compile(
        r"\b(?:принял(?:а)?\s+задачу|берусь|запускаю|проверяю|смотрю|начинаю|"
        r"исправляю|добавляю|обновляю|вношу|прогоняю|работаю\s+над|планирую|"
        r"(?:сейчас|теперь|сначала|дальше)\s+(?:разбер\w*|запущу|запускаю|добавлю|"
        r"проверю|исправлю|обновлю|внесу|сделаю|создам|подготовлю|перепишу|настрою|"
        r"соберу|удалю|перенесу|прогоню)|"
        r"(?:исправлю|запущу|добавлю|проверю|обновлю|внесу|сделаю|создам|"
        r"подготовлю|перепишу|настрою|соберу|удалю|перенесу|прогоню)|"
        r"буду\s+(?:добавлять|проверять|исправлять|менять|обновлять|запускать|"
        r"создавать|готовить|переписывать|настраивать|собирать))\b",
        re.IGNORECASE,
    )
    _NO_CHANGE_RE = re.compile(
        r"\b(?:публикац\w*|запрос\w*|изменени\w*|правок|депло\w*)\b.{0,100}"
        r"\bне\s+(?:было|произошло|выполнялось|делалось)\b",
        re.IGNORECASE,
    )
    _WAITING_RE = re.compile(
        r"\b(?:жду|пришли|пришлите|уточни|уточните|нужно\s+(?:уточнить|прислать)|"
        r"не\s+вижу\s+(?:файл|вложен)|после\s+этого\s+продолжу|готов\s+продолжить)\b",
        re.IGNORECASE,
    )
    _CANCEL_RE = re.compile(
        r"^(?:давай\s+(?:вс[её]\s+|это\s+)?(?:откатим|отменим)|"
        r"(?:(?:так|ладно)[,\s]+)?(?:вс[её][,\s]+)?(?:стоп|отбой)|"
        r"(?:отмени|откати)\s+(?:вс[её]|эти|последние|текущие)\s+(?:изменения|правки))\b",
        re.IGNORECASE,
    )
    _SENSITIVE_OPERATION_RE = re.compile(
        r"(?:\b[A-Za-z]:[\\/]|(?<!\w)/(?:root|home|opt|etc|srv|var/(?:lib|log|www))(?:/|\b)|"
        r"\b(?:SHA256|MD5):[A-Za-z0-9:+/=.-]{12,}|\b(?:ssh-(?:rsa|ed25519)|ED25519|RSA)\b)",
        re.IGNORECASE,
    )
    _SCOPE_LOCK_RE = re.compile(
        r"\b(?:только|only)\s+(?P<kind>pr|issue|задач(?:а|у|и)?)\s*#?\s*(?P<number>\d+)\b",
        re.IGNORECASE,
    )
    _SCOPE_ID_RE = re.compile(
        r"\b(?P<kind>pr|issue|задач(?:а|у|и)?)\s*#?\s*(?P<number>\d+)\b",
        re.IGNORECASE,
    )
    _FOCUS_STOPWORDS = {
        "автоматически", "без", "будет", "быть", "весь", "всё", "для", "его", "если",
        "задача", "затем", "или", "как", "который", "можно", "надо", "нужно", "после",
        "проверь", "проверка", "работа", "сделай", "только", "чтобы", "через", "этого", "этот",
        "add", "check", "create", "fix", "implement", "only", "task", "the", "with",
    }

    def __init__(self) -> None:
        self.privacy = PrivacyScanner()

    def build_documents(self, topics: Iterable[TopicDocument]) -> list[StoryDocument]:
        source = list(topics)
        documents = [self._render(topic) for topic in source if self._is_editorial_material(topic)]
        if len({item.relative_path for item in documents}) != len(documents):
            raise RuntimeError("Story path collision")
        if len({item.topic_id for item in documents}) != len(documents):
            raise RuntimeError("Story topic ID collision")
        return sorted(documents, key=lambda item: item.relative_path.as_posix().casefold())

    def _render(self, topic: TopicDocument) -> StoryDocument:
        source_hash = hashlib.sha256(topic.text.encode("utf-8")).hexdigest()
        users = [item for item in topic.source_messages if item.role == "user" and item.text.strip()]
        codex = [item for item in topic.source_messages if item.role == "codex" and item.text.strip()]

        opening = self._opening_detail(topic, users)
        constraints = self._select_constraints(users)
        pivots = self._select_pivots(users[1:] if users else [])
        outcome = self._select_outcome(topic, users, codex)
        confirmed_outcome = self._has_confirmed_outcome(codex, outcome)
        progress = self._select_progress(topic, users, codex, outcome)

        story_valid = bool(users and topic.title.strip())
        if topic.closed and not topic.cancelled:
            story_valid = story_valid and confirmed_outcome
        publishable = bool(topic.publishable and story_valid and topic.closed and not topic.cancelled)

        if topic.cancelled:
            result_label = "отменено пользователем"
        elif not topic.closed:
            result_label = "работа продолжается"
        elif confirmed_outcome:
            result_label = "результат зафиксирован"
        else:
            result_label = "результат не зафиксирован"

        lines = [
            f"# {topic.title}",
            "",
            f"Проект: {topic.project_name}",
            f"Дата: {topic.date}",
            f"Тема-ID: t-{topic.topic_id}",
            f"Чат: {topic.session_id}",
        ]
        if topic.dialog_title:
            lines.append(f"Тема диалога: {topic.dialog_title}")
        if topic.git_branch:
            lines.append(f"Ветка Git: {topic.git_branch}")
        lines.extend(
            [
                f"Граница: {topic.boundary_reason or 'topic'}",
                f"Формат: редакторский рассказ",
                f"Версия формата: {self.FORMAT_VERSION}",
                f"Источник-хеш: sha256:{source_hash}",
                f"Реплик источника: {len(topic.source_messages)}",
                f"Статус: {'закрыта' if topic.closed else 'открыта'}",
                f"Результат: {result_label}",
                f"Автопубликация: {'разрешена' if publishable else 'запрещена'}",
                "",
                "## История",
                "",
                f"Работа началась с задачи: «{self._quote(topic.title)}».",
            ]
        )

        if opening:
            lines.extend(["", f"Исходная постановка уточняла: «{self._quote(opening)}»."])
        if constraints:
            lines.extend(["", "Ключевые условия: " + self._join_quotes(constraints) + "."])
        if pivots:
            lines.extend(["", "По ходу работы запрос уточнился: " + self._join_quotes(pivots) + "."])
        if progress:
            lines.extend(["", "В процессе зафиксировали: " + self._join_quotes(progress) + "."])

        if topic.cancelled:
            lines.extend(
                [
                    "",
                    "После проверки работу по этой ветке остановили по просьбе пользователя. "
                    "Завершённый результат для публикации не заявляется.",
                ]
            )
        elif not topic.closed:
            suffix = f" Последний зафиксированный шаг: «{self._quote(outcome)}»." if outcome else ""
            lines.extend(
                [
                    "",
                    "На момент снимка тема ещё не закрыта, поэтому её нельзя автоматически выдавать за готовый кейс."
                    + suffix,
                ]
            )
        elif confirmed_outcome:
            lines.extend(["", "К финалу работа получила подтверждённый результат."])
        else:
            lines.extend(["", "В архиве нет подтверждённого результата, поэтому тема оставлена для ручной проверки."])

        lines.extend(["", "## Итог", ""])
        if topic.cancelled:
            lines.append("Тема отменена. Автоматическая публикация запрещена.")
        elif not topic.closed:
            lines.append("Работа продолжается. Итоговый результат пока не подтверждён.")
        elif confirmed_outcome:
            lines.append(outcome)
        else:
            lines.append("Подтверждённый итог в исходном диалоге не найден.")

        text = "\n".join(lines).strip() + "\n"
        text, _ = self.privacy.scan_and_mask(text, topic.relative_path.as_posix())
        self._validate_story(text)
        return StoryDocument(
            relative_path=topic.relative_path,
            text=text,
            project_name=topic.project_name,
            date=topic.date,
            session_id=topic.session_id,
            topic_id=topic.topic_id,
            title=topic.title,
            publishable=publishable,
            source_hash=source_hash,
        )

    def _is_editorial_material(self, topic: TopicDocument) -> bool:
        """Keep greetings, typos and acknowledgements in ai-logs, not in content inbox."""
        users = [item for item in topic.source_messages if item.role == "user" and item.text.strip()]
        if not users:
            return False
        anchored = topic.boundary_reason in {
            "task_request",
            "question_request",
            "substantive_request",
            "explicit_topic_marker",
            "idle_gap",
            "post_cancel_request",
            "safety_chunk",
        }
        for message in users:
            clean = self._inline(message.text)
            if self._WEAK_RE.fullmatch(clean):
                continue
            words = re.findall(r"[\w-]+", clean, flags=re.UNICODE)
            if anchored and len(clean) >= 12 and len(words) >= 2:
                return True
            if len(clean) >= 24 and len(words) >= 4:
                return True
        return False

    def _focus_terms(
        self,
        topic: TopicDocument,
        users: list[TopicSourceMessage],
    ) -> set[str]:
        seed = topic.title
        if users:
            seed += " " + self._inline(users[0].text)[:500]
        normalized = seed.casefold().replace("_", " ").replace("-", " ")
        return {
            token
            for token in re.findall(r"[\w#]+", normalized, flags=re.UNICODE)
            if len(token) >= 3 and token not in self._FOCUS_STOPWORDS
        }

    def _focus_overlap(self, fragment: str, focus: set[str]) -> int:
        if not focus:
            return 0
        normalized = fragment.casefold().replace("_", " ").replace("-", " ")
        tokens = set(re.findall(r"[\w#]+", normalized, flags=re.UNICODE))
        return len(tokens & focus)

    def _scope_locks(self, users: list[TopicSourceMessage]) -> dict[str, set[str]]:
        locks: dict[str, set[str]] = {}
        for message in users:
            for match in self._SCOPE_LOCK_RE.finditer(message.text):
                kind = match.group("kind").casefold()
                if kind.startswith("задач"):
                    kind = "задача"
                locks.setdefault(kind, set()).add(match.group("number"))
        return locks

    def _violates_scope(self, fragment: str, locks: dict[str, set[str]]) -> bool:
        if not locks:
            return False
        for match in self._SCOPE_ID_RE.finditer(fragment):
            kind = match.group("kind").casefold()
            if kind.startswith("задач"):
                kind = "задача"
            allowed = locks.get(kind)
            if allowed and match.group("number") not in allowed:
                return True
        return False

    def _opening_detail(self, topic: TopicDocument, users: list[TopicSourceMessage]) -> str:
        if not users:
            return ""
        title_key = self._key(topic.title)
        for fragment in self._fragments(users[0].text):
            fragment_key = self._key(fragment)
            if fragment_key and fragment_key != title_key and fragment_key not in title_key and title_key not in fragment_key:
                return fragment
        return ""

    def _select_constraints(self, users: list[TopicSourceMessage]) -> list[str]:
        candidates: list[str] = []
        for message in users[:3]:
            for fragment in self._fragments(message.text):
                if self._CONSTRAINT_RE.search(fragment):
                    candidates.append(fragment)
        return self._unique(candidates, 2)

    def _select_pivots(self, users: list[TopicSourceMessage]) -> list[str]:
        candidates: list[tuple[int, int, str]] = []
        for index, message in enumerate(users):
            clean_message = self._inline(message.text)
            if self._CANCEL_RE.match(clean_message):
                continue
            for fragment in self._fragments(message.text):
                if self._WEAK_RE.fullmatch(fragment):
                    continue
                score = 2 if self._PIVOT_RE.search(fragment) else 0
                if len(fragment) >= 60:
                    score += 1
                if score:
                    candidates.append((score, index, fragment))
        selected = sorted(candidates, key=lambda item: (-item[0], -item[1]))[:2]
        return [item[2] for item in sorted(selected, key=lambda item: item[1])]

    def _select_progress(
        self,
        topic: TopicDocument,
        users: list[TopicSourceMessage],
        messages: list[TopicSourceMessage],
        outcome: str,
    ) -> list[str]:
        candidates: list[tuple[int, int, str]] = []
        outcome_key = self._key(outcome)
        focus = self._focus_terms(topic, users)
        scope_locks = self._scope_locks(users)
        for index, message in enumerate(messages):
            for fragment in self._fragments(message.text):
                fragment_key = self._key(fragment)
                if fragment_key == outcome_key or (fragment_key and fragment_key in outcome_key):
                    continue
                score = 0
                if self._IN_PROGRESS_RE.search(fragment):
                    continue
                if self._WAITING_RE.search(fragment):
                    continue
                if self._violates_scope(fragment, scope_locks):
                    continue
                if self._RESULT_RE.search(fragment) or self._NO_CHANGE_RE.search(fragment):
                    score += 2
                if self._PIVOT_RE.search(fragment):
                    score += 1
                score += min(2, self._focus_overlap(fragment, focus))
                if score > 0:
                    candidates.append((score, index, fragment))
        selected = sorted(candidates, key=lambda item: (-item[0], -item[1]))[:2]
        return [item[2] for item in sorted(selected, key=lambda item: item[1])]

    def _select_outcome(
        self,
        topic: TopicDocument,
        users: list[TopicSourceMessage],
        messages: list[TopicSourceMessage],
    ) -> str:
        candidates: list[tuple[int, int, list[str]]] = []
        focus = self._focus_terms(topic, users)
        scope_locks = self._scope_locks(users)
        for index, message in enumerate(messages):
            fragments = self._fragments(message.text)
            if not fragments:
                continue
            result_fragments = [
                fragment
                for fragment in fragments
                if not self._WAITING_RE.search(fragment)
                and not self._IN_PROGRESS_RE.search(fragment)
                and not self._violates_scope(fragment, scope_locks)
                and (self._RESULT_RE.search(fragment) or self._NO_CHANGE_RE.search(fragment))
            ]
            if not result_fragments:
                continue
            overlap = sum(self._focus_overlap(fragment, focus) for fragment in result_fragments)
            score = len(result_fragments) * 100 + overlap * 20 + min(index, 20)
            candidates.append((score, index, result_fragments))
        if not candidates:
            return ""
        _, _, fragments = max(candidates, key=lambda item: (item[0], item[1]))
        result_indexes = [
            index
            for index, fragment in enumerate(fragments)
            if self._RESULT_RE.search(fragment) or self._NO_CHANGE_RE.search(fragment)
            if not self._WAITING_RE.search(fragment)
            if not self._IN_PROGRESS_RE.search(fragment)
        ]
        if result_indexes:
            selected_indexes = set(result_indexes[:5])
            first = result_indexes[0]
            for index in range(first + 1, min(len(fragments), first + 4)):
                selected_indexes.add(index)
            selected = [fragments[index].rstrip(" .;:") for index in sorted(selected_indexes)[:5]]
        else:
            selected = []
        return self._clip("; ".join(selected), self.EXCERPT_LIMIT * 3)

    def _has_confirmed_outcome(self, messages: list[TopicSourceMessage], outcome: str) -> bool:
        if not outcome or self._IN_PROGRESS_RE.search(outcome) or self._WAITING_RE.search(outcome):
            return False
        return bool(self._RESULT_RE.search(outcome) or self._NO_CHANGE_RE.search(outcome))

    def _fragments(self, text: str) -> list[str]:
        value = re.sub(r"```.*?```", " ", str(text or ""), flags=re.DOTALL)
        value = re.sub(r"<[^>]+>.*?</[^>]+>", " ", value, flags=re.DOTALL)
        value = re.sub(r"\[([^\]\n]+)\]\([^\n)]*\)", r"\1", value)
        lines: list[str] = []
        for raw_line in value.replace("\r\n", "\n").splitlines():
            line = re.sub(r"^\s*(?:#{1,6}\s+|[-*+]\s+|\d+[.)]\s+|>\s*)", "", raw_line).strip()
            line = line.replace("`", "")
            if (
                not line
                or self._GENERIC_LINE_RE.fullmatch(line)
                or self._SECTION_LABEL_RE.fullmatch(line)
                or (line.endswith(":") and len(line.split()) <= 8)
                or self._METADATA_LINE_RE.match(line)
            ):
                continue
            lines.append(line)

        fragments: list[str] = []
        for line in lines:
            for raw_fragment in re.split(r"(?<=[.!?])\s+|\s*;\s*", line):
                fragment = self._inline(raw_fragment).strip(" -–—•")
                fragment = re.sub(
                    r"^(?:цель|важно|ограничение|условие|результат|итог|текст|задача)\s*:\s*",
                    "",
                    fragment,
                    flags=re.IGNORECASE,
                )
                if (
                    len(fragment) < 3
                    or self._WEAK_RE.fullmatch(fragment)
                    or self._SENSITIVE_OPERATION_RE.search(fragment)
                ):
                    continue
                fragments.append(self._clip(fragment, self.EXCERPT_LIMIT))
        return self._unique(fragments, 12)

    def _join_quotes(self, values: list[str]) -> str:
        return "; ".join(f"«{self._quote(value)}»" for value in values)

    def _quote(self, value: str) -> str:
        return self._inline(value).strip(" .").replace("»", "”").replace("«", "“")

    def _clip(self, value: str, limit: int) -> str:
        clean = self._inline(value)
        if len(clean) <= limit:
            return clean
        clipped = clean[: limit + 1]
        return (clipped.rsplit(" ", 1)[0] or clean[:limit]).rstrip(" ,;:-") + "…"

    def _inline(self, value: str) -> str:
        return re.sub(r"\s+", " ", str(value or "")).strip()

    def _key(self, value: str) -> str:
        return re.sub(r"[^\w]+", " ", self._inline(value).casefold()).strip()

    def _unique(self, values: list[str], limit: int) -> list[str]:
        result: list[str] = []
        seen: set[str] = set()
        for value in values:
            key = self._key(value)
            if not key or key in seen:
                continue
            seen.add(key)
            result.append(value)
            if len(result) >= limit:
                break
        return result

    def _validate_story(self, text: str) -> None:
        forbidden = ("## Диалог", "### Пользователь", "### Codex")
        if any(marker in text for marker in forbidden):
            raise ValueError("Raw transcript marker leaked into story document")
        if "## История" not in text or "## Итог" not in text:
            raise ValueError("Story document is missing required sections")
        if "\x00" in text or not text.strip():
            raise ValueError("Invalid story document")
