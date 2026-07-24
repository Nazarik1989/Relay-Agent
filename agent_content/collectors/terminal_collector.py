from __future__ import annotations

from pathlib import Path

from agent_content.models import TerminalLog


class TerminalCollector:
    def __init__(self, logs_dir: str = "terminal-logs") -> None:
        self.logs_dir = Path(logs_dir)

    def collect(self, target_date: str) -> list[TerminalLog]:
        if not self.logs_dir.exists():
            return []

        logs: list[TerminalLog] = []
        for path in sorted(self.logs_dir.rglob("*")):
            if path.suffix.lower() not in {".md", ".txt", ".log"}:
                continue
            if target_date not in path.name:
                continue
            text = path.read_text(encoding="utf-8", errors="replace").strip()
            if not text:
                continue
            logs.append(TerminalLog(path=str(path), title=f"Терминал {path.stem}", text=text))
        return logs

    def append_note(self, target_date: str, text: str) -> Path:
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        path = self.logs_dir / f"{target_date}.md"
        with path.open("a", encoding="utf-8") as file:
            if path.stat().st_size == 0:
                file.write(f"# Terminal log {target_date}\n\n")
            file.write(text.rstrip() + "\n\n")
        return path
