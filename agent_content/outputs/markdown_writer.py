from __future__ import annotations

from pathlib import Path


class MarkdownWriter:
    def write(self, path: Path, pack: dict) -> Path:
        lines: list[str] = []
        lines.append(f"# Контент-пакет за {pack['date']}")
        lines.append("")
        lines.append("## Краткий recap дня")
        lines.append(pack["recap"]["what_happened"])
        lines.append("")
        lines.append(f"Главный сюжет: {pack['recap']['main_story']}")
        lines.append("")
        lines.append(f"Главная мысль: {pack['recap']['main_thought']}")
        lines.append("")
        lines.append("## Лучший формат на сегодня")
        best = pack["best_format"]
        lines.append(f"Формат: {best['format']}")
        lines.append("")
        lines.append(f"Стиль: {best['tone']}")
        lines.append("")
        lines.append(f"Почему: {best['why']}")
        lines.append("")
        lines.append("## Сторис")
        for story in pack["stories"]:
            lines.append("")
            lines.append(f"### {story['title']}")
            lines.append(f"Текст: {story['text']}")
            lines.append("")
            lines.append(f"Визуал: {story['visual']}")
            lines.append("")
            lines.append(f"Стиль: {story['style']}")
            lines.append("")
            lines.append(f"Цель: {story['goal']}")
        lines.append("")
        lines.append("## Рилс")
        for reel in pack["reels"]:
            lines.append("")
            lines.append(f"Hook: {reel['hook']}")
            lines.append("")
            for scene in reel["scenes"]:
                lines.append(f"Сцена {scene['scene']}:")
                lines.append(f"Кадр: {scene['frame']}")
                lines.append(f"Текст на экране: {scene['on_screen_text']}")
                lines.append(f"Закадровый голос: {scene['voice_over']}")
                lines.append("")
            lines.append(f"Подпись: {reel['caption']}")
            lines.append("")
            lines.append(f"CTA: {reel['cta']}")
        lines.append("")
        lines.append("## Пост")
        lines.append(pack["post"])
        lines.append("")
        lines.append("## Hooks")
        for hook in pack["hooks"]:
            lines.append(f"- {hook}")
        lines.append("")
        lines.append("## Что нельзя публиковать без проверки")
        for item in pack["do_not_publish"]:
            lines.append(f"- {item['note']} Источник: {item['source']}.")
        lines.append("")
        lines.append("## События и оценки")
        for event in pack["events"]:
            lines.append(f"- {event['title']} — {event['score']}/100. Причины: {', '.join(event['score_reasons'])}.")

        path.write_text("\n".join(lines).strip() + "\n", encoding="utf-8")
        return path
