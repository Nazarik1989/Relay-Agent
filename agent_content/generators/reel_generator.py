from __future__ import annotations

from agent_content.config.tone_profiles import TONE_PROFILES
from agent_content.models import WorkEvent
from agent_content.utils import clip


class ReelGenerator:
    def generate(self, event: WorkEvent) -> list[dict]:
        profile = TONE_PROFILES[event.tone]
        return [
            {
                "hook": self._hook(event),
                "tone": profile["name"],
                "scenes": [
                    {
                        "scene": 1,
                        "frame": "VS Code с открытым рабочим чатом Codex.",
                        "on_screen_text": "Обычный рабочий диалог",
                        "voice_over": "Снаружи это выглядит как переписка с помощником, но внутри уже есть сюжет дня.",
                    },
                    {
                        "scene": 2,
                        "frame": "Крупно показать вопрос, решение или поворот в диалоге.",
                        "on_screen_text": clip(event.title, 56),
                        "voice_over": f"Главный момент: {clip(event.summary, 150)}",
                    },
                    {
                        "scene": 3,
                        "frame": "Показать черновик content pack в markdown.",
                        "on_screen_text": "AI превращает рабочий разговор в историю",
                        "voice_over": "Агент берет не техническую историю репозитория, а смысл рабочего разговора: что обсуждали, что решили, где был поворот.",
                    },
                    {
                        "scene": 4,
                        "frame": "Финальный экран с выбором: сторис, рилс, пост.",
                        "on_screen_text": "Не выдумывать контент. Доставать настоящий.",
                        "voice_over": "Самое ценное уже происходит в работе. Нужно только аккуратно заметить это и превратить в человеческий текст.",
                    },
                ],
                "caption": "Собираю локального AI-летописца работы: он читает рабочий чат с Codex, заметки и важные решения, а потом предлагает идеи для контента.",
                "cta": "А вы бы доверили AI собирать идеи для контента из вашей рабочей переписки?",
            }
        ]

    def _hook(self, event: WorkEvent) -> str:
        if event.kind == "bug":
            return "Рабочая проблема из чата внезапно стала хорошей идеей для рилса."
        if event.kind == "refactor":
            return "Иногда история дня прячется не в коде, а в том, как мы объясняем себе изменение."
        if event.kind == "insight":
            return "Иногда лучший контент дня прячется в одной рабочей мысли."
        return "Я делаю бота, который читает рабочий диалог и сам предлагает, что можно рассказать людям."
