from __future__ import annotations

import os
import re
from pathlib import Path
from shutil import copy2, rmtree
from uuid import uuid4


class NazAiInbox:
    def __init__(self, local_path: str | None = None, inbox_dir: str | None = None) -> None:
        raw_path = local_path or os.environ.get("NAZAI_LOCAL_PATH") or str(Path("..") / "Naz-AI_Bot")
        self.local_path = Path(raw_path).expanduser()
        raw_inbox = inbox_dir or os.environ.get("NAZAI_INBOX_DIR") or "content_inbox/agent_content"
        self.inbox_dir = Path(raw_inbox)
        if not self.inbox_dir.is_absolute():
            self.inbox_dir = self.local_path / self.inbox_dir

    def write_package(self, target_date: str, files: list[Path], metadata: dict) -> dict[str, object]:
        del metadata  # The inbox is intentionally text-only; no sidecar manifest.
        self._assert_safe_inbox()
        self.inbox_dir.mkdir(parents=True, exist_ok=True)
        day_dir = self.inbox_dir / target_date
        staging_dir = self.inbox_dir / f".{target_date}.staging-{uuid4().hex}"
        backup_dir = self.inbox_dir / f".{target_date}.backup-{uuid4().hex}"
        staging_dir.mkdir(parents=False, exist_ok=False)

        copied: list[Path] = []
        try:
            for path in files:
                if path.suffix.casefold() != ".md" or not path.is_file() or path.is_symlink():
                    continue
                target = staging_dir / path.name
                if target.exists():
                    raise RuntimeError(f"Duplicate inbox document name: {path.name}")
                copy2(path, target)
                self._validate_text_document(target)
                copied.append(target)

            if not copied:
                raise ValueError(f"No Markdown chat documents for {target_date}")

            if day_dir.exists():
                day_dir.replace(backup_dir)
            staging_dir.replace(day_dir)
            if backup_dir.exists():
                rmtree(backup_dir)
        except Exception:
            if staging_dir.exists():
                rmtree(staging_dir)
            if backup_dir.exists() and not day_dir.exists():
                backup_dir.replace(day_dir)
            raise

        return {"day_dir": day_dir, "documents": [day_dir / path.name for path in copied]}

    def write_archive(self, documents_by_date: dict[str, list[Path]]) -> dict[str, object]:
        """Atomically replace the complete local inbox with validated Markdown."""
        self._assert_safe_inbox()
        parent = self.inbox_dir.parent
        parent.mkdir(parents=True, exist_ok=True)
        staging_dir = parent / f".{self.inbox_dir.name}.staging-{uuid4().hex}"
        backup_dir = parent / f".{self.inbox_dir.name}.backup-{uuid4().hex}"
        staging_writer = NazAiInbox(local_path=str(self.local_path), inbox_dir=str(staging_dir))
        document_count = 0
        date_count = 0

        try:
            for target_date, files in sorted(documents_by_date.items()):
                markdown_files = [path for path in files if path.suffix.casefold() == ".md"]
                if not markdown_files:
                    continue
                result = staging_writer.write_package(target_date, markdown_files, {})
                document_count += len(result["documents"])
                date_count += 1
            if not document_count:
                raise ValueError("No Markdown chat documents to import into NazAI")

            if self.inbox_dir.exists():
                self.inbox_dir.replace(backup_dir)
            staging_dir.replace(self.inbox_dir)
            if backup_dir.exists():
                rmtree(backup_dir)
        except Exception:
            if staging_dir.exists():
                rmtree(staging_dir)
            if backup_dir.exists() and not self.inbox_dir.exists():
                backup_dir.replace(self.inbox_dir)
            raise

        return {
            "inbox_dir": self.inbox_dir,
            "date_count": date_count,
            "document_count": document_count,
        }

    def write_documents(self, documents: dict[Path, str]) -> dict[str, object]:
        """Atomically replace the inbox with an explicit text-only folder tree."""
        self._assert_safe_inbox()
        if not documents:
            raise ValueError("No Markdown documents to import into NazAI")

        parent = self.inbox_dir.parent
        parent.mkdir(parents=True, exist_ok=True)
        staging_dir = parent / f".{self.inbox_dir.name}.staging-{uuid4().hex}"
        backup_dir = parent / f".{self.inbox_dir.name}.backup-{uuid4().hex}"
        staging_dir.mkdir(parents=False, exist_ok=False)
        written: list[Path] = []

        try:
            for raw_relative, text in sorted(documents.items(), key=lambda item: item[0].as_posix().casefold()):
                relative = Path(raw_relative)
                self._validate_relative_document_path(relative)
                target = staging_dir / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                if target.exists():
                    raise RuntimeError(f"Duplicate inbox document path: {relative.as_posix()}")
                target.write_text(text, encoding="utf-8")
                self._validate_text_document(target)
                written.append(target)

            if self.inbox_dir.exists():
                self.inbox_dir.replace(backup_dir)
            staging_dir.replace(self.inbox_dir)
            if backup_dir.exists():
                rmtree(backup_dir)
        except Exception:
            if staging_dir.exists():
                rmtree(staging_dir)
            if backup_dir.exists() and not self.inbox_dir.exists():
                backup_dir.replace(self.inbox_dir)
            raise

        date_pattern = re.compile(r"^\d{4}-\d{2}-\d{2}$")
        dates = {
            part
            for relative in documents
            for part in Path(relative).parts
            if date_pattern.fullmatch(part)
        }
        projects = {Path(relative).parts[0] for relative in documents if len(Path(relative).parts) >= 2}
        return {
            "inbox_dir": self.inbox_dir,
            "date_count": len(dates),
            "project_count": len(projects),
            "document_count": len(written),
            "documents": [self.inbox_dir / path.relative_to(staging_dir) for path in written],
        }

    def _assert_safe_inbox(self) -> None:
        resolved = self.inbox_dir.resolve()
        if resolved == Path(resolved.anchor):
            raise RuntimeError(f"Refusing unsafe inbox path: {self.inbox_dir}")

    def _validate_relative_document_path(self, path: Path) -> None:
        if path.is_absolute() or not path.parts or any(part in {"", ".", ".."} for part in path.parts):
            raise ValueError(f"Unsafe inbox document path: {path}")
        if path.suffix.casefold() != ".md":
            raise ValueError(f"Inbox accepts Markdown only: {path}")

    def _validate_text_document(self, path: Path) -> None:
        text = path.read_text(encoding="utf-8", errors="strict")
        if not text.strip():
            raise ValueError(f"Empty inbox document: {path.name}")
        if "\x00" in text:
            raise ValueError(f"NUL byte in inbox document: {path.name}")
