from __future__ import annotations

from dataclasses import asdict

from agent_content.analyzers.tone_selector import ToneSelector
from agent_content.generators.hook_generator import HookGenerator
from agent_content.generators.post_generator import PostGenerator
from agent_content.generators.reel_generator import ReelGenerator
from agent_content.generators.story_generator import StoryGenerator
from agent_content.models import Note, PrivacyFinding, TerminalLog, WorkEvent


class ContentPackGenerator:
    def __init__(self, tone_selector: ToneSelector, story_count: int = 7) -> None:
        self.tone_selector = tone_selector
        self.story_count = story_count

    def generate(
        self,
        target_date: str,
        notes: list[Note],
        ai_logs: list[Note],
        terminal_logs: list[TerminalLog],
        events: list[WorkEvent],
        privacy_findings: list[PrivacyFinding],
    ) -> dict:
        primary = sorted(events, key=lambda event: event.score, reverse=True)[0]
        return {
            "date": target_date,
            "recap": self._recap(primary, events),
            "best_format": self._best_format(primary),
            "events": [asdict(event) for event in events],
            "stories": StoryGenerator().generate(primary, self.story_count),
            "reels": ReelGenerator().generate(primary),
            "post": PostGenerator().generate(primary),
            "hooks": HookGenerator().generate(primary),
            "do_not_publish": self._safety_list(privacy_findings),
            "raw_context": {
                "notes": [asdict(note) for note in notes],
                "ai_logs": [asdict(log) for log in ai_logs],
                "terminal_logs": [asdict(log) for log in terminal_logs],
            },
        }

    def _recap(self, primary: WorkEvent, events: list[WorkEvent]) -> dict[str, str]:
        return {
            "what_happened": f"Собрано рабочих событий: {len(events)}. Главный сигнал: {primary.title}.",
            "main_story": primary.summary,
            "main_thought": "Рабочий диалог можно превращать не в сухой отчет, а в понятный контентный сюжет.",
        }

    def _best_format(self, event: WorkEvent) -> dict[str, str]:
        if event.kind in {"bug", "process"}:
            fmt = "рилс или серия сторис"
            reason = "есть процесс, напряжение и визуальный потенциал закулисья"
        elif event.kind == "refactor":
            fmt = "до/после или экспертная заметка"
            reason = "видна трансформация: было сложнее, стало понятнее"
        elif event.kind == "insight":
            fmt = "короткий пост или философская заметка"
            reason = "главная ценность в мысли, а не в демонстрации экрана"
        else:
            fmt = "сторис + короткий пост"
            reason = "можно быстро объяснить пользу и оставить след для аудитории"
        return {
            "format": fmt,
            "tone": self.tone_selector.describe(event.tone),
            "why": reason,
        }

    def _safety_list(self, findings: list[PrivacyFinding]) -> list[dict[str, str]]:
        if not findings:
            return [
                {
                    "kind": "manual_review",
                    "source": "general",
                    "note": "Явных секретов не найдено, но перед публикацией стоит проверить скриншоты и названия проектов.",
                }
            ]
        return [
            {
                "kind": finding.kind,
                "source": finding.source,
                "note": f"Найдено и замаскировано: {finding.replacement}",
            }
            for finding in findings
        ]
