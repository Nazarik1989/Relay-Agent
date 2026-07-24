from __future__ import annotations

from pathlib import Path


class TodayPickWriter:
    def write(self, path: Path, pack: dict) -> Path:
        path.write_text(self._to_markdown(pack), encoding="utf-8")
        return path

    def _to_markdown(self, pack: dict) -> str:
        primary = sorted(pack["events"], key=lambda event: event["score"], reverse=True)[0]
        stories = pack["stories"][:3]
        lines: list[str] = []
        lines.append(f"# Что постить сегодня — {pack['date']}")
        lines.append("")
        lines.append("## Решение")
        lines.append(f"Формат: {pack['best_format']['format']}")
        lines.append("")
        lines.append(f"Почему: {pack['best_format']['why']}")
        lines.append("")
        lines.append("## Главный сюжет")
        lines.append(primary["summary"])
        lines.append("")
        lines.append("## Готовый пост")
        lines.append(pack["post"])
        lines.append("")
        lines.append("## Сторис на 3 кадра")
        for story in stories:
            lines.append("")
            lines.append(f"### {story['title']}")
            lines.append(story["text"])
            lines.append("")
            lines.append(f"Визуал: {story['visual']}")
        lines.append("")
        lines.append("## Хук")
        lines.append(pack["hooks"][0])
        lines.append("")
        lines.append("## Перед публикацией")
        for item in pack["do_not_publish"][:5]:
            lines.append(f"- {item['note']}")
        return "\n".join(lines).strip() + "\n"
