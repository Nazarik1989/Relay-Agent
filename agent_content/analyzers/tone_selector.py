from __future__ import annotations

from agent_content.config.tone_profiles import DEFAULT_TONE_ROTATION, TONE_PROFILES
from agent_content.models import WorkEvent


class ToneSelector:
    def __init__(self, recent_tones: list[str] | None = None) -> None:
        self.recent_tones = recent_tones or []

    def select(self, event: WorkEvent) -> str:
        text = f"{event.title} {event.summary}".lower()
        preferred = self._preferred_by_event(event.kind, text)
        for tone in preferred + DEFAULT_TONE_ROTATION:
            if tone not in self.recent_tones:
                return tone
        return preferred[0] if preferred else DEFAULT_TONE_ROTATION[0]

    def describe(self, tone_key: str) -> str:
        profile = TONE_PROFILES.get(tone_key, TONE_PROFILES[DEFAULT_TONE_ROTATION[0]])
        return f"{profile['name']} — {profile['voice']}"

    def _preferred_by_event(self, kind: str, text: str) -> list[str]:
        if kind == "bug" or any(word in text for word in ["bug", "ошибка", "слом"]):
            return ["behind_the_scenes", "techno_hooligan", "almost_meme"]
        if kind == "refactor" or any(word in text for word in ["refactor", "архитект", "упрост"]):
            return ["scrupulous_builder", "mini_lesson", "ai_translator"]
        if kind == "feature":
            return ["ai_translator", "future_builder", "behind_the_scenes"]
        if kind == "insight":
            return ["future_builder", "mini_lesson", "scrupulous_builder"]
        return ["ai_translator", "behind_the_scenes", "mini_lesson"]
