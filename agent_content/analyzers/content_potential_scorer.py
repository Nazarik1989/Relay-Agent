from __future__ import annotations

from agent_content.models import WorkEvent


class ContentPotentialScorer:
    def score(self, event: WorkEvent) -> WorkEvent:
        score = 20
        reasons: list[str] = []
        text = f"{event.title} {event.summary} {' '.join(event.signals)}".lower()

        checks = [
            (["bug", "error", "failed", "exception", "ошибка", "слом", "fix"], 18, "conflict or problem"),
            (["refactor", "architecture", "clean", "рефактор", "архитект", "упрост"], 14, "visible before/after change"),
            (["agent", "ai", "bot", "автомат", "модель", "codex"], 16, "AI/automation angle"),
            (["note", "insight", "вывод", "понял", "идея"], 12, "personal insight"),
            (["ui", "visual", "screen", "demo", "картин", "визуал"], 10, "visual potential"),
            (["user", "client", "польз", "обычн", "клиент", "зрител"], 10, "human value"),
        ]
        for keywords, points, reason in checks:
            if any(keyword in text for keyword in keywords):
                score += points
                reasons.append(reason)

        if event.source.startswith("ai_log:"):
            score += 28
            reasons.append("live context from Codex chat")
        elif event.source.startswith("manual_note:"):
            score += 20
            reasons.append("manual meaning captured")
        elif event.source.startswith("terminal_log:"):
            score += 12
            reasons.append("concrete terminal signal")

        if len(event.signals) >= 3:
            score += 8
            reasons.append("enough facts for a story")
        if event.kind == "fallback":
            score -= 10
            reasons.append("not enough work signals")

        event.score = max(0, min(100, score))
        event.score_reasons = reasons or ["calm daily recap material"]
        return event
