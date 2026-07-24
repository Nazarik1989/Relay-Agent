from __future__ import annotations

from agent_content.models import Note, TerminalLog, WorkEvent
from agent_content.utils import clip


class EventAnalyzer:
    def analyze(self, notes: list[Note], ai_logs: list[Note], terminal_logs: list[TerminalLog] | None = None) -> list[WorkEvent]:
        events: list[WorkEvent] = []
        events.extend(self._events_from_notes(ai_logs, "codex_chat"))
        events.extend(self._events_from_notes(notes, "manual_note"))
        events.extend(self._events_from_terminal(terminal_logs or []))

        if not events:
            events.append(
                WorkEvent(
                    title="No Codex work chat found",
                    summary=(
                        "For this date the agent did not find a Codex VS Code chat, manual note, or terminal note. "
                        "There is no work-chat material to turn into a chronicle."
                    ),
                    kind="fallback",
                    signals=["No Codex chat summary for this date"],
                    source="system:no_codex_chat",
                )
            )
        return events

    def _events_from_terminal(self, logs: list[TerminalLog]) -> list[WorkEvent]:
        events: list[WorkEvent] = []
        for log in logs:
            clean_text = self._clean_note_text(log.text)
            events.append(
                WorkEvent(
                    title=log.title,
                    summary=clip(clean_text, 700),
                    kind=self._guess_kind(clean_text),
                    signals=self._extract_signals(clean_text),
                    source=f"terminal_log:{log.path}",
                )
            )
        return events

    def _events_from_notes(self, notes: list[Note], source_kind: str) -> list[WorkEvent]:
        events: list[WorkEvent] = []
        for note in notes:
            clean_text = self._clean_note_text(note.text)
            events.append(
                WorkEvent(
                    title=note.title,
                    summary=clip(clean_text, 900),
                    kind=self._guess_kind(clean_text),
                    signals=self._extract_signals(clean_text),
                    source=f"{source_kind}:{note.path}",
                )
            )
        return events

    def _guess_kind(self, text: str) -> str:
        lowered = text.lower()
        if any(word in lowered for word in ["bug", "fix", "error", "ошибка", "слом", "почини"]):
            return "bug"
        if any(word in lowered for word in ["refactor", "architecture", "рефактор", "архитект", "упрост"]):
            return "refactor"
        if any(word in lowered for word in ["insight", "понял", "вывод", "идея"]):
            return "insight"
        if any(word in lowered for word in ["feature", "фича", "добавил", "создал"]):
            return "feature"
        return "process"

    def _extract_signals(self, text: str) -> list[str]:
        signals: list[str] = []
        for line in text.splitlines():
            clean = line.strip(" -#\t")
            if len(clean) >= 12:
                signals.append(clip(clean, 180))
            if len(signals) == 8:
                break
        return signals or [clip(text, 180)]

    def _clean_note_text(self, text: str) -> str:
        cleaned: list[str] = []
        for line in text.splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            stripped = stripped.lstrip("#").strip()
            stripped = stripped.lstrip("-*").strip()
            cleaned.append(stripped)
        return " ".join(cleaned)
