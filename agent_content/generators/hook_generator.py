from __future__ import annotations

from agent_content.models import WorkEvent


class HookGenerator:
    def generate(self, event: WorkEvent) -> list[str]:
        title = event.title.lower()
        return [
            "Рабочий чат с Codex уже содержит готовый сюжет дня.",
            f"Сегодня рабочий диалог превратился в историю: {title}.",
            "AI помогает не придумывать контент, а замечать то, что уже произошло в работе.",
            "Самое ценное часто спрятано не в отчете, а в вопросах, решениях и поворотах рабочего разговора.",
            "Я собираю не сухую хронику задач, а человеческую память проекта.",
        ]
