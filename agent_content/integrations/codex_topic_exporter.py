from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable
from zoneinfo import ZoneInfo


@dataclass(frozen=True)
class TopicSourceMessage:
    timestamp: str
    role: str
    text: str


@dataclass(frozen=True)
class TopicDocument:
    relative_path: Path
    text: str
    project_name: str
    date: str
    session_id: str
    topic_id: str
    title: str
    publishable: bool
    closed: bool = False
    cancelled: bool = False
    dialog_title: str = ""
    git_branch: str = ""
    boundary_reason: str = ""
    source_messages: tuple[TopicSourceMessage, ...] = field(default_factory=tuple, repr=False)


@dataclass(frozen=True)
class _Event:
    message: Any
    summary: Any
    timestamp: datetime | None
    input_order: int

    @property
    def role(self) -> str:
        return str(getattr(self.message, "role", "") or "").strip().casefold()

    @property
    def text(self) -> str:
        return str(getattr(self.message, "text", "") or "")


@dataclass
class _Episode:
    session_id: str
    project_name: str
    dialog_title: str
    git_branch: str
    boundary_reason: str
    events: list[_Event] = field(default_factory=list)
    anchor: _Event | None = None
    codex_after_anchor: bool = False
    cancelled: bool = False

    def append(self, event: _Event) -> None:
        self.events.append(event)
        if self.anchor is not None and event.role == "codex":
            self.codex_after_anchor = True

    @property
    def has_user(self) -> bool:
        return any(event.role == "user" for event in self.events)

    @property
    def has_codex(self) -> bool:
        return any(event.role == "codex" for event in self.events)


class CodexTopicExporter:
    """Build an extractive, deterministic topic view of visible Codex chat.

    The exporter deliberately does not try to infer semantic similarity. A
    topic is a chronological episode opened by an observable user request.
    Every input message is assigned to exactly one episode and rendered in
    full; weak continuations are retained but are not auto-publishable.
    """

    IDLE_BOUNDARY = timedelta(minutes=90)
    QUIET_CLOSE = timedelta(hours=2)
    SUBSTANTIVE_CHARS = 160
    TITLE_CHARS = 78
    MAX_EPISODE_MESSAGES = 60
    MAX_EPISODE_USER_TURNS = 10

    _TASK_START_RE = re.compile(
        r"^(?:срочно\s+)?(?:"
        r"выполни|проведи|проверь|добавь|исправь|сделай|создай|реализуй|настрой|"
        r"подключи|собери|зафиксируй|опубликуй|удали|отмени|откати|"
        r"перенеси|проанализируй|протестируй|разберись|найди|обнови|"
        r"разработай|подскажи"
        r")\b",
        re.IGNORECASE,
    )
    _EXPLICIT_TOPIC_RE = re.compile(
        r"\b(?:новая\s+задача|отдельная\s+задача|перейд[её]м\s+к)\b",
        re.IGNORECASE,
    )
    _METADATA_LINE_RE = re.compile(
        r"^(?:проект|github|gitlab|репозиторий|рабоч(?:ий|ая)\s+"
        r"(?:каталог|папка)|локальн(?:ый|ая)\s+(?:путь|папка))\s*:",
        re.IGNORECASE,
    )
    _GENERIC_HEADING_RE = re.compile(
        r"^(?:goal|context|scope|request|task|objective|"
        r"цель|контекст|задача|требования|что\s+нужно)\s*:?[\s#]*$",
        re.IGNORECASE,
    )
    _BULLET_RE = re.compile(r"^\s*(?:[-*+]\s+|\d+[.)]\s+)", re.MULTILINE)
    _ACTION_INTENT_RE = re.compile(
        r"\b(?:надо|нужно|необходимо|требуется)\s+(?:это\s+)?(?:"
        r"исправить|поправить|починить|устранить|убрать|удалить|добавить|"
        r"проверить|перепроверить|настроить|обновить|переделать|повторить"
        r")\b",
        re.IGNORECASE,
    )
    _QUESTION_TASK_RE = re.compile(
        r"^(?:а\s+)?(?:как|где|почему|зачем|можно\s+ли|можешь|сможешь|что\s+нужно)\b",
        re.IGNORECASE,
    )
    _SOFT_TASK_RE = re.compile(
        r"\b(?:хочу|давай|пусть|можно|можешь|сможешь|надо|нужно)\b.{0,50}\b(?:"
        r"сделать|добавить|исправить|проверить|настроить|обновить|подключить|создать|"
        r"убрать|удалить|перенести|собрать|отправлять|присылать|публиковать|исключить)\b|"
        r"\b(?:мне\s+нужно|я\s+хочу|хочу|необходимо)\b.{4,}|"
        r"\bдавай\b.{0,40}\b(?:сдела\w*|постав\w*|запланир\w*|нагенер\w*|выбер\w*|"
        r"авториз\w*|обнов\w*|запуст\w*|откро\w*|повтор\w*)\b|"
        r"\b(?:добавляй|исправляй|проверяй|настраивай|обновляй|присылай|отправляй|"
        r"публикуй|исключай)\b",
        re.IGNORECASE,
    )
    _CONTINUATION_TASK_RE = re.compile(
        r"\b(?:у\s+нас\s+не\s+закончена\s+работа|верн[её]мся\s+к|следующ(?:ая|ий)\s+(?:задача|шаг))\b",
        re.IGNORECASE,
    )
    _GENERIC_TITLE_RE = re.compile(
        r"^(?:так\s+)?(?:давай\s+)?(?:соберись|мне\s+нужно\s+решение|ну\s+что|что\s+делать|продолжим)\b",
        re.IGNORECASE,
    )
    _ACKNOWLEDGEMENT_RE = re.compile(
        r"^(?:привет|спасибо|круто|отлично|понял|поняла|ясно|ок(?:ей)?|"
        r"вс[её]\s+норм\??|о[,!]?\s+пришло|да|нет|ага)[.!?\s]*$",
        re.IGNORECASE,
    )
    _TITLE_PREAMBLE_RE = re.compile(
        r"^(?:войд\s+справился\b|ты\s+прав\b|теперь\s+добили\s+полностью\b)",
        re.IGNORECASE,
    )
    _TITLE_ACTIONS = (
        (re.compile(r"^(?:пожалуйста[,\s]+)?(?:выполни|выполнить)\s+", re.IGNORECASE), "Выполнение: "),
        (re.compile(r"^(?:пожалуйста[,\s]+)?(?:добавь|добавить)\s+", re.IGNORECASE), "Добавление: "),
        (re.compile(r"^(?:пожалуйста[,\s]+)?(?:исправь|исправить)\s+", re.IGNORECASE), "Исправление: "),
        (re.compile(r"^(?:пожалуйста[,\s]+)?(?:проверь|проверить|проведи\s+проверку)\s+", re.IGNORECASE), "Проверка: "),
        (re.compile(r"^(?:пожалуйста[,\s]+)?(?:реализуй|реализовать)\s+", re.IGNORECASE), "Реализация: "),
        (re.compile(r"^(?:пожалуйста[,\s]+)?(?:настрой|настроить)\s+", re.IGNORECASE), "Настройка: "),
        (re.compile(r"^(?:пожалуйста[,\s]+)?(?:обнови|обновить)\s+", re.IGNORECASE), "Обновление: "),
        (re.compile(r"^(?:пожалуйста[,\s]+)?(?:подключи|подключить)\s+", re.IGNORECASE), "Подключение: "),
        (re.compile(r"^(?:пожалуйста[,\s]+)?(?:создай|создать)\s+", re.IGNORECASE), "Создание: "),
        (re.compile(r"^(?:пожалуйста[,\s]+)?(?:собери|собрать)\s+", re.IGNORECASE), "Сборка: "),
        (re.compile(r"^(?:пожалуйста[,\s]+)?(?:удали|удалить|убери|убрать)\s+", re.IGNORECASE), "Удаление: "),
        (re.compile(r"^(?:пожалуйста[,\s]+)?(?:перенеси|перенести)\s+", re.IGNORECASE), "Перенос: "),
        (re.compile(r"^(?:пожалуйста[,\s]+)?(?:проанализируй|проанализировать)\s+", re.IGNORECASE), "Анализ: "),
        (re.compile(r"^(?:пожалуйста[,\s]+)?(?:протестируй|протестировать)\s+", re.IGNORECASE), "Тестирование: "),
        (re.compile(r"^(?:пожалуйста[,\s]+)?(?:найди|найти)\s+", re.IGNORECASE), "Поиск: "),
    )
    _COLLABORATIVE_CANCEL_RE = re.compile(
        r"^(?:(?:нет|не|ладно|ок(?:ей)?)[,.:;!\s-]+)?"
        r"давай\s+(?:вс[её]\s+|это\s+)?(?:откатим|отменим)\b",
        re.IGNORECASE,
    )
    _STOP_CANCEL_RE = re.compile(
        r"^(?:(?:так|ладно|ок(?:ей)?)[,.:;!\s-]+)?"
        r"(?:вс[её][,.:;!\s-]+)?(?:стоп|отбой|хватит)[.!…\s]*$",
        re.IGNORECASE,
    )
    _UNDO_CURRENT_CANCEL_RE = re.compile(
        r"^(?:отмени|откати)\s+(?:вс[её]|эти|последние|текущие)\s+"
        r"(?:изменения|правки)\b",
        re.IGNORECASE,
    )
    _ABANDON_CANCEL_RE = re.compile(
        r"^(?:останавливаем(?:ся)?|отказываемся\s+от\s+(?:этого|идеи|варианта|задачи)|"
        r"вс[её][,.:;!\s-]+не\s+надо|не\s+надо(?:\s+(?:это|его|её|их|больше))?"
        r"(?:\s+делать)?)\s*[.!…]*$",
        re.IGNORECASE,
    )

    def __init__(
        self,
        timezone: str = "Europe/Moscow",
        now: datetime | None = None,
    ) -> None:
        self.timezone = ZoneInfo(timezone)
        if now is None:
            self.now = datetime.now(tz=self.timezone)
        elif now.tzinfo is None:
            self.now = now.replace(tzinfo=self.timezone)
        else:
            self.now = now.astimezone(self.timezone)

    def build_documents(self, summaries: Iterable[Any]) -> list[TopicDocument]:
        indexed = list(enumerate(summaries))
        sessions: dict[str, list[tuple[int, Any]]] = {}
        for input_index, summary in indexed:
            session_id = str(getattr(summary, "session_id", "") or "").strip()
            if not session_id:
                continue
            sessions.setdefault(session_id, []).append((input_index, summary))

        documents: list[TopicDocument] = []
        for session_id, session_summaries in sessions.items():
            documents.extend(self._build_session_documents(session_id, session_summaries))
        return sorted(documents, key=lambda item: str(item.relative_path).casefold())

    def _build_session_documents(
        self,
        session_id: str,
        indexed_summaries: list[tuple[int, Any]],
    ) -> list[TopicDocument]:
        ordered_summaries = sorted(
            indexed_summaries,
            key=lambda item: (
                str(getattr(item[1], "date", "") or ""),
                item[0],
            ),
        )
        events: list[_Event] = []
        event_order = 0
        for _, summary in ordered_summaries:
            for message in list(getattr(summary, "messages", []) or []):
                events.append(
                    _Event(
                        message=message,
                        summary=summary,
                        timestamp=self._parse_timestamp(getattr(message, "timestamp", "")),
                        input_order=event_order,
                    )
                )
                event_order += 1
        if not events:
            return []

        episodes = self._partition(session_id, events)
        archived = any(bool(getattr(summary, "archived", False)) for _, summary in ordered_summaries)
        documents: list[TopicDocument] = []
        for index, episode in enumerate(episodes):
            closed = index < len(episodes) - 1 or archived or self._quietly_closed(episode)
            documents.append(self._render_document(episode, closed))
        return documents

    def _partition(self, session_id: str, events: list[_Event]) -> list[_Episode]:
        episodes: list[_Episode] = []
        current: _Episode | None = None
        previous_user_time: datetime | None = None
        current_date = ""

        for event in events:
            event_date = str(getattr(event.summary, "date", "") or "")
            if current is not None and current.events and event_date and event_date != current_date:
                episodes.append(current)
                current = None
            if current is None:
                current = self._new_episode(session_id, event, "session_start")
                current_date = event_date

            if event.role != "user":
                current.append(event)
                continue

            cancellation = self._is_cancellation(event.text)
            anchor_candidate, anchor_reason = self._anchor_reason(event.text)
            if cancellation:
                anchor_candidate, anchor_reason = False, ""
            idle_boundary = self._is_idle_boundary(previous_user_time, event.timestamp)
            safety_boundary = bool(
                len(current.events) >= self.MAX_EPISODE_MESSAGES
                or sum(item.role == "user" for item in current.events) >= self.MAX_EPISODE_USER_TURNS
            )
            start_new = False
            boundary_reason = anchor_reason or "continuation"

            if current.events:
                if cancellation:
                    start_new = False
                elif current.cancelled and self._is_meaningful_request(event.text):
                    start_new = True
                    anchor_candidate = True
                    boundary_reason = "post_cancel_request"
                elif safety_boundary and self._is_meaningful_request(event.text):
                    start_new = True
                    anchor_candidate = True
                    boundary_reason = "safety_chunk"
                elif idle_boundary and anchor_candidate:
                    start_new = True
                    boundary_reason = "idle_gap"
                elif anchor_candidate and current.anchor is not None and current.codex_after_anchor:
                    start_new = True
                elif anchor_candidate and current.anchor is None and current.has_user and current.has_codex:
                    start_new = True

            if start_new:
                episodes.append(current)
                current = self._new_episode(session_id, event, boundary_reason)

            current.append(event)
            if cancellation:
                current.cancelled = True
            if anchor_candidate and current.anchor is None:
                current.anchor = event
                if current.boundary_reason in {"session_start", "continuation"}:
                    current.boundary_reason = anchor_reason

            if event.timestamp is not None:
                previous_user_time = event.timestamp

        if current is not None and current.events:
            episodes.append(current)
        return episodes

    def _new_episode(self, session_id: str, event: _Event, reason: str) -> _Episode:
        summary = event.summary
        return _Episode(
            session_id=session_id,
            project_name=str(getattr(summary, "project_name", "") or "project").strip() or "project",
            dialog_title=self._inline(getattr(summary, "dialog_title", "")),
            git_branch=self._inline(getattr(summary, "git_branch", "")),
            boundary_reason=reason,
        )

    def _anchor_reason(self, text: str) -> tuple[bool, str]:
        clean = text.strip()
        if not clean:
            return False, ""
        if self._EXPLICIT_TOPIC_RE.search(clean):
            return True, "explicit_topic_marker"
        if self._starts_with_task(clean):
            return True, "task_request"
        if self._ACTION_INTENT_RE.search(clean):
            return True, "task_request"
        if self._SOFT_TASK_RE.search(clean):
            return True, "task_request"
        if self._CONTINUATION_TASK_RE.search(clean):
            return True, "task_request"
        if self._QUESTION_TASK_RE.search(clean) and self._is_meaningful_request(clean):
            return True, "question_request"
        nonempty_lines = [line for line in clean.splitlines() if line.strip()]
        bullet_count = len(self._BULLET_RE.findall(clean))
        if (
            len(clean) >= self.SUBSTANTIVE_CHARS
            or len(nonempty_lines) >= 3
            or bullet_count >= 2
        ):
            return True, "substantive_request"
        return False, ""

    def _is_meaningful_request(self, text: str) -> bool:
        clean = self._inline(text)
        words = re.findall(r"[\w-]+", clean, flags=re.UNICODE)
        return bool(
            len(clean) >= 20
            and len(words) >= 3
            and not self._ACKNOWLEDGEMENT_RE.fullmatch(clean)
        )

    def _starts_with_task(self, text: str) -> bool:
        # Strip common Markdown markers before applying the anchored verb list.
        clean = re.sub(r"^\s*(?:[#>*_`-]+\s*)+", "", text)
        return bool(self._TASK_START_RE.match(clean))

    def _is_cancellation(self, text: str) -> bool:
        clean = re.sub(r"\s+", " ", text).strip()
        if not clean:
            return False
        return bool(
            self._COLLABORATIVE_CANCEL_RE.match(clean)
            or self._STOP_CANCEL_RE.match(clean)
            or self._UNDO_CURRENT_CANCEL_RE.match(clean)
            or self._ABANDON_CANCEL_RE.match(clean)
        )

    def _is_idle_boundary(
        self,
        previous_user_time: datetime | None,
        current_user_time: datetime | None,
    ) -> bool:
        if previous_user_time is None or current_user_time is None:
            return False
        return current_user_time - previous_user_time >= self.IDLE_BOUNDARY

    def _quietly_closed(self, episode: _Episode) -> bool:
        timestamps = [event.timestamp for event in episode.events if event.timestamp is not None]
        if not timestamps:
            return False
        last_timestamp = max(timestamps)
        return self.now >= last_timestamp and self.now - last_timestamp >= self.QUIET_CLOSE

    def _render_document(self, episode: _Episode, closed: bool) -> TopicDocument:
        title_event = episode.anchor or next(
            (event for event in episode.events if event.role == "user" and event.text.strip()),
            None,
        )
        if title_event is not None:
            title = self._literal_title(title_event.text)
        elif episode.dialog_title:
            title = self._literal_title(episode.dialog_title)
        else:
            title = "Продолжение диалога"
        if (self._GENERIC_TITLE_RE.search(title) or "скрыт]" in title.casefold()) and episode.dialog_title:
            title = self._literal_title(episode.dialog_title)

        identity_event = episode.anchor or title_event or episode.events[0]
        topic_id = self._topic_id(episode.session_id, identity_event)
        date = self._event_date(identity_event)
        hhmm = self._event_hhmm(identity_event)
        slug = self._safe_slug(title, fallback="prodolzhenie-dialoga", limit=72)
        project_dir = self._safe_project_name(episode.project_name)
        filename = f"{date}-{hhmm}--{slug}--t-{topic_id}.md"
        relative_path = Path(project_dir) / date / filename
        publishable = bool(
            closed
            and episode.anchor is not None
            and episode.codex_after_anchor
            and not episode.cancelled
        )

        lines = [
            f"# {title}",
            "",
            f"Проект: {episode.project_name}",
            f"Дата: {date}",
            f"Тема-ID: t-{topic_id}",
            f"Чат: {episode.session_id}",
        ]
        if episode.dialog_title:
            lines.append(f"Тема диалога: {episode.dialog_title}")
        if episode.git_branch:
            lines.append(f"Ветка Git: {episode.git_branch}")
        lines.extend(
            [
                f"Граница: {episode.boundary_reason}",
                f"Статус: {'закрыта' if closed else 'открыта'}",
                f"Результат: {'отменено пользователем' if episode.cancelled else 'без явной отмены'}",
                f"Автопубликация: {'разрешена' if publishable else 'запрещена'}",
                f"Реплик: {len(episode.events)}",
                "Источник заголовка: дословный фрагмент пользовательского запроса",
                "",
                "## Диалог",
            ]
        )

        for event in episode.events:
            if event.role == "user":
                speaker = "Пользователь"
            elif event.role == "codex":
                speaker = "Codex"
            else:
                speaker = event.role or "Сообщение"
            timestamp = self._display_timestamp(event)
            heading = f"### {speaker}"
            if timestamp:
                heading += f" · {timestamp}"
            lines.extend(["", heading, "", event.text])

        return TopicDocument(
            relative_path=relative_path,
            text="\n".join(lines).strip() + "\n",
            project_name=episode.project_name,
            date=date,
            session_id=episode.session_id,
            topic_id=topic_id,
            title=title,
            publishable=publishable,
            closed=closed,
            cancelled=episode.cancelled,
            dialog_title=episode.dialog_title,
            git_branch=episode.git_branch,
            boundary_reason=episode.boundary_reason,
            source_messages=tuple(
                TopicSourceMessage(
                    timestamp=str(getattr(event.message, "timestamp", "") or ""),
                    role=event.role,
                    text=event.text,
                )
                for event in episode.events
            ),
        )

    def _topic_id(self, session_id: str, event: _Event) -> str:
        timestamp = str(getattr(event.message, "timestamp", "") or "")
        normalized_text = re.sub(r"\s+", " ", event.text).strip()
        raw = "\0".join((session_id, timestamp, event.role, normalized_text))
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]

    def _event_date(self, event: _Event) -> str:
        if event.timestamp is not None:
            return event.timestamp.astimezone(self.timezone).date().isoformat()
        raw_date = str(getattr(event.summary, "date", "") or "")
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw_date):
            return raw_date
        return "undated"

    def _event_hhmm(self, event: _Event) -> str:
        if event.timestamp is None:
            return "0000"
        return event.timestamp.astimezone(self.timezone).strftime("%H%M")

    def _display_timestamp(self, event: _Event) -> str:
        if event.timestamp is None:
            return str(getattr(event.message, "timestamp", "") or "").strip()
        return event.timestamp.astimezone(self.timezone).isoformat(timespec="seconds")

    def _parse_timestamp(self, value: Any) -> datetime | None:
        if isinstance(value, datetime):
            parsed = value
        else:
            text = str(value or "").strip()
            if not text:
                return None
            try:
                parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
            except ValueError:
                return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(self.timezone)

    def _literal_title(self, text: Any) -> str:
        raw_lines = [line.strip() for line in str(text or "").splitlines() if line.strip()]
        candidates = [
            line
            for line in raw_lines
            if not self._METADATA_LINE_RE.match(line)
            and not self._GENERIC_HEADING_RE.match(line)
            and not self._TITLE_PREAMBLE_RE.match(line)
            and not (line.endswith(":") and len(line.split()) <= 8)
        ]
        task_candidates = [line for line in candidates if self._starts_with_task(line)]
        value = (
            task_candidates[0]
            if task_candidates
            else candidates[0]
            if candidates
            else raw_lines[0]
            if raw_lines
            else "Продолжение диалога"
        )
        value = re.sub(r"^\s*(?:[#>*_`-]+\s*)+", "", value)
        value = self._inline(value)
        sentence = re.match(r"^(.{12,}?[.!?])(?:\s|$)", value)
        if sentence:
            value = sentence.group(1)
        value = re.sub(r"^(?:привет|здравствуй(?:те)?)[,!\s]+", "", value, flags=re.IGNORECASE)
        value = re.split(r"\s*(?:;|\s+[—–]\s+|,\s+(?:чтобы|затем|после\s+чего))\s*", value, maxsplit=1)[0]
        for pattern, replacement in self._TITLE_ACTIONS:
            if pattern.search(value):
                value = pattern.sub(replacement, value, count=1)
                break
        if len(value) > self.TITLE_CHARS:
            clipped = value[: self.TITLE_CHARS + 1]
            value = clipped.rsplit(" ", 1)[0] or value[: self.TITLE_CHARS]
            value = re.sub(
                r"\s+(?:в|во|на|с|со|к|по|для|из|от|до|как|чтобы|и|или|а|но|мне|нужен|нужна|нужно)$",
                "",
                value,
                flags=re.IGNORECASE,
            )
            value = value.rstrip(" ,;:-") + "…"
        return value.strip(" -") or "Продолжение диалога"

    def _safe_slug(self, value: str, fallback: str, limit: int) -> str:
        normalized = unicodedata.normalize("NFC", self._inline(value)).casefold()
        normalized = re.sub(r"[^\w.-]+", "-", normalized, flags=re.UNICODE)
        normalized = re.sub(r"-{2,}", "-", normalized).strip(" .-")
        if len(normalized) > limit:
            normalized = normalized[:limit].rstrip(" .-")
        return normalized or fallback

    def _safe_project_name(self, value: str) -> str:
        normalized = unicodedata.normalize("NFC", self._inline(value))
        normalized = re.sub(r"[^\w.-]+", "-", normalized, flags=re.UNICODE)
        normalized = re.sub(r"-{2,}", "-", normalized).strip(" .-")
        if len(normalized) > 80:
            normalized = normalized[:80].rstrip(" .-")
        return normalized or "project"

    def _inline(self, value: Any) -> str:
        return re.sub(r"\s+", " ", str(value or "")).strip()
