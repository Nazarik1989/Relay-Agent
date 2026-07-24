from __future__ import annotations

from pathlib import Path

from agent_content.models import Note


class AiLogsCollector:
    def __init__(self, logs_dir: str) -> None:
        self.logs_dir = Path(logs_dir)

    def collect(self, target_date: str) -> list[Note]:
        if not self.logs_dir.exists():
            return []
        logs: list[Note] = []
        for path in sorted(self.logs_dir.rglob("*")):
            if path.suffix.lower() not in {".md", ".txt"} or target_date not in path.name:
                continue
            text = path.read_text(encoding="utf-8", errors="replace").strip()
            if text:
                logs.append(Note(path=str(path), title=f"AI-сессия {path.stem}", text=text))
        return logs
