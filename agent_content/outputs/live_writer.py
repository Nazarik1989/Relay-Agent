from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path


class LiveWriter:
    def write(self, outputs_dir: Path, payload: dict) -> tuple[Path, Path]:
        md_path = outputs_dir / "live-chronicle.md"
        json_path = outputs_dir / "live-chronicle.json"
        md_path.write_text(self._to_markdown(payload), encoding="utf-8")
        json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return md_path, json_path

    def _to_markdown(self, payload: dict) -> str:
        lines: list[str] = [
            "# Живая летопись работы",
            "",
            f"Обновлено: {payload['updated_at']}",
            "",
            f"Проектов под наблюдением: {len(payload['projects'])}",
            "",
        ]

        for project in payload["projects"]:
            lines.append(f"## {project['name']}")
            lines.append("")
            lines.append("Источник летописи: рабочий чат с Codex, ручные заметки и важные терминальные заметки.")
            lines.append("Техническая история репозитория не используется как сюжетный источник.")
            lines.append("")

            lines.append("Главные события:")
            for event in project["events"][:5]:
                lines.append(f"- {event['title']} - {event['score']}/100")
                lines.append(f"  {event['summary']}")
            lines.append("")

            lines.append("Быстрые хуки:")
            for hook in project["hooks"][:3]:
                lines.append(f"- {hook}")
            lines.append("")

            lines.append("Перед публикацией:")
            for item in project["do_not_publish"][:5]:
                lines.append(f"- {item['note']}")
            lines.append("")

        lines.append("## Как читать")
        lines.append("Это черновая live-летопись по рабочему диалогу с Codex и заметкам, а не технический отчет.")
        return "\n".join(lines).strip() + "\n"


def now_local_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")
