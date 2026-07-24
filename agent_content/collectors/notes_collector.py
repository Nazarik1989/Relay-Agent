from __future__ import annotations

from pathlib import Path

from agent_content.models import Note


class NotesCollector:
    def __init__(self, notes_dir: str) -> None:
        self.notes_dir = Path(notes_dir)

    def collect(self, target_date: str) -> list[Note]:
        if not self.notes_dir.exists():
            return []

        notes: list[Note] = []
        for path in sorted(self.notes_dir.rglob("*")):
            if path.suffix.lower() not in {".md", ".txt"}:
                continue
            if path.name.startswith("example"):
                continue
            if target_date not in path.name:
                continue
            text = path.read_text(encoding="utf-8", errors="replace").strip()
            if not text:
                continue
            title = self._extract_title(path, text)
            notes.append(Note(path=str(path), title=title, text=text))
        return notes

    def _extract_title(self, path: Path, text: str) -> str:
        for line in text.splitlines():
            stripped = line.strip()
            if stripped.startswith("#"):
                return stripped.lstrip("#").strip()
        return path.stem.replace("-", " ").strip().title()
