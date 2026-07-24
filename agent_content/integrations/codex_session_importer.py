from __future__ import annotations

import json
import re
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from shutil import rmtree
from uuid import uuid4
from zoneinfo import ZoneInfo

from agent_content.analyzers.privacy_scanner import PrivacyScanner
from agent_content.utils import clip


@dataclass
class CodexMessage:
    timestamp: str
    role: str
    text: str


@dataclass
class CodexAction:
    timestamp: str
    kind: str
    text: str
    detail: str = ""


@dataclass
class CodexSessionSummary:
    date: str
    project_name: str
    project_path: str
    session_id: str
    source_path: Path
    messages: list[CodexMessage]
    actions: list[CodexAction]
    dialog_title: str = ""
    git_branch: str = ""
    archived: bool = False


class CodexSessionImporter:
    def __init__(
        self,
        sessions_dir: str | Path | Iterable[str | Path] | None = None,
        timezone: str = "Europe/Moscow",
        session_index_path: str | Path | None = None,
    ) -> None:
        if sessions_dir is None:
            codex_home = Path.home() / ".codex"
            roots = [codex_home / "sessions", codex_home / "archived_sessions"]
        elif isinstance(sessions_dir, (str, Path)):
            roots = [Path(sessions_dir)]
        else:
            roots = [Path(item) for item in sessions_dir]
        self.session_roots = roots
        # Compatibility for callers that used the old public attribute.
        self.sessions_dir = roots[0]
        self.session_index_path = Path(session_index_path) if session_index_path else Path.home() / ".codex" / "session_index.jsonl"
        try:
            self.timezone = ZoneInfo(timezone)
        except Exception:
            self.timezone = timezone_from_offset()
        self.privacy = PrivacyScanner()
        self.session_titles = self._load_session_titles()

    def import_sessions(
        self,
        projects: list[dict],
        output_root: str | Path,
        clear: bool = False,
        format: str = "detailed",
        layout: str = "central",
    ) -> list[Path]:
        summaries = self.collect_summaries(projects)
        return self.write_summaries(
            summaries,
            projects,
            output_root,
            clear=clear,
            format=format,
            layout=layout,
        )

    def write_summaries(
        self,
        summaries: list[CodexSessionSummary],
        projects: list[dict],
        output_root: str | Path,
        clear: bool = False,
        format: str = "detailed",
        layout: str = "central",
    ) -> list[Path]:
        final_output_root = Path(output_root)
        staging_root: Path | None = None
        backup_root: Path | None = None
        if clear and layout == "central":
            final_output_root.parent.mkdir(parents=True, exist_ok=True)
            staging_root = final_output_root.parent / f".{final_output_root.name}.staging-{uuid4().hex}"
            backup_root = final_output_root.parent / f".{final_output_root.name}.backup-{uuid4().hex}"
            output_root = staging_root
        else:
            output_root = final_output_root
        if layout == "central":
            output_root.mkdir(parents=True, exist_ok=True)

        written: list[Path] = []
        cleaned_project_dirs: set[str] = set()
        try:
            for summary in summaries:
                project_dir = self._target_dir(summary, projects, output_root, layout)
                if clear and layout == "project":
                    key = str(project_dir.resolve())
                    if key not in cleaned_project_dirs and project_dir.exists():
                        self._clean_output_root(project_dir)
                    cleaned_project_dirs.add(key)
                project_dir.mkdir(parents=True, exist_ok=True)

                topic_slug = self._topic_slug(summary.dialog_title)
                stem = f"{summary.date}-codex-{topic_slug}--{self._safe_name(summary.session_id)}"
                if format in {"brief", "both"}:
                    suffix = "-brief" if format == "both" else ""
                    target = project_dir / f"{stem}{suffix}.md"
                    target.write_text(self._to_brief_markdown(summary), encoding="utf-8")
                    written.append(target)
                if format in {"detailed", "both"}:
                    target = project_dir / f"{stem}.md"
                    target.write_text(self._to_detailed_markdown(summary), encoding="utf-8")
                    written.append(target)

            if staging_root is not None and backup_root is not None:
                if final_output_root.exists():
                    final_output_root.replace(backup_root)
                staging_root.replace(final_output_root)
                written = [final_output_root / path.relative_to(staging_root) for path in written]
                if backup_root.exists():
                    rmtree(backup_root)
        except Exception:
            if staging_root is not None and staging_root.exists():
                rmtree(staging_root)
            if backup_root is not None and backup_root.exists() and not final_output_root.exists():
                backup_root.replace(final_output_root)
            raise
        return written

    def collect_summaries(self, projects: list[dict]) -> list[CodexSessionSummary]:
        summaries: list[CodexSessionSummary] = []
        seen_paths: set[str] = set()
        for root in self.session_roots:
            if not root.exists():
                continue
            for path in sorted(root.rglob("*.jsonl")):
                path_key = str(path.resolve()).casefold()
                if path_key in seen_paths:
                    continue
                seen_paths.add(path_key)
                summaries.extend(self._parse_session(path, projects))

        # During archival the same thread can briefly exist in both roots.
        # Keep the fuller snapshot for each thread/day pair.
        unique: dict[tuple[str, str], CodexSessionSummary] = {}
        for summary in summaries:
            key = (summary.session_id, summary.date)
            existing = unique.get(key)
            if existing is None or len(summary.messages) > len(existing.messages):
                unique[key] = summary
        return sorted(
            unique.values(),
            key=lambda item: (item.date, item.project_name.casefold(), item.session_id),
        )

    def _load_session_titles(self) -> dict[str, str]:
        titles: dict[str, str] = {}
        if not self.session_index_path.exists():
            return titles
        with self.session_index_path.open("r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                try:
                    item = json.loads(line)
                except json.JSONDecodeError:
                    continue
                session_id = str(item.get("id") or "").strip()
                title = self._literal_title(str(item.get("thread_name") or ""))
                if session_id and title:
                    titles[session_id] = title
        return titles

    def _dialog_title(self, session_id: str, messages: list[CodexMessage]) -> str:
        indexed = self.session_titles.get(session_id, "")
        if indexed:
            return indexed
        for message in messages:
            if message.role != "user":
                continue
            title = self._literal_title(message.text)
            if title:
                return title
        return "Продолжение диалога"

    def _literal_title(self, value: str, limit: int = 96) -> str:
        repaired = self._repair_mojibake(self._normalize_text(value))
        repaired, _ = self.privacy.scan_and_mask(repaired, str(self.session_index_path))
        ignored_prefixes = (
            "проект:",
            "рабочий каталог:",
            "репозиторий:",
            "repository:",
            "github:",
            "документы:",
        )
        for raw_line in repaired.splitlines():
            line = re.sub(r"^[#>*\-\d.)\s]+", "", raw_line).strip()
            if not line or line.casefold().startswith(ignored_prefixes):
                continue
            if len(line) <= limit:
                return line
            shortened = line[:limit].rsplit(" ", 1)[0].rstrip(" ,.;:-")
            return shortened or line[:limit].rstrip()
        return ""

    def _parse_session(self, path: Path, projects: list[dict]) -> list[CodexSessionSummary]:
        cwd = ""
        session_id = path.stem
        first_timestamp = ""
        is_subagent = False
        git_branch = ""
        messages: list[CodexMessage] = []
        actions: list[CodexAction] = []

        # Rollouts can be hundreds of MiB because tool/world-state records are
        # embedded in JSONL. Decode only records that can contain visible chat.
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                item_type = self._top_level_type(line)
                if item_type not in {"session_meta", "event_msg"}:
                    continue
                try:
                    item = json.loads(line)
                except json.JSONDecodeError:
                    continue

                timestamp = item.get("timestamp") or ""
                if timestamp and not first_timestamp:
                    first_timestamp = timestamp

                payload = item.get("payload") or {}
                if item_type == "session_meta":
                    cwd = payload.get("cwd") or cwd
                    git_data = payload.get("git") if isinstance(payload.get("git"), dict) else {}
                    git_branch = str(git_data.get("branch") or git_branch).strip()
                    # In subagent records `session_id` points at the parent.
                    # `id` is the real identity of this rollout.
                    session_id = payload.get("id") or payload.get("session_id") or session_id
                    if self._is_subagent_session(payload):
                        is_subagent = True
                        break
                    continue

                event_type = payload.get("type")
                if event_type == "user_message":
                    text = self._clean_user_message(payload.get("message") or "")
                    role = "user"
                elif event_type == "agent_message":
                    text = self._clean_agent_message(payload.get("message") or "")
                    role = "codex"
                else:
                    continue

                text = self._repair_mojibake(text)
                text, _ = self.privacy.scan_and_mask(text, str(path))
                if text:
                    messages.append(CodexMessage(timestamp=timestamp, role=role, text=text))

        if is_subagent:
            return []

        project = self._match_project(cwd, projects) or self._project_from_cwd(cwd)
        if not project:
            return []

        fallback_date = self._local_date(first_timestamp or path.stat().st_mtime)
        clean_messages = self._compress_messages(messages)
        dialog_title = self._dialog_title(session_id, clean_messages)
        messages_by_date: dict[str, list[CodexMessage]] = defaultdict(list)
        for message in clean_messages:
            target_date = self._local_date(message.timestamp) if message.timestamp else fallback_date
            messages_by_date[target_date].append(message)

        actions_by_date: dict[str, list[CodexAction]] = defaultdict(list)
        for action in self._compress_actions(actions):
            target_date = self._local_date(action.timestamp) if action.timestamp else fallback_date
            actions_by_date[target_date].append(action)

        return [
            CodexSessionSummary(
                date=target_date,
                project_name=project["name"],
                project_path=project["repo_path"],
                session_id=session_id,
                source_path=path,
                messages=day_messages,
                actions=actions_by_date.get(target_date, []),
                dialog_title=dialog_title,
                git_branch=git_branch,
                archived="archived_sessions" in {part.casefold() for part in path.parts},
            )
            for target_date, day_messages in sorted(messages_by_date.items())
            if day_messages
        ]

    def _top_level_type(self, line: str) -> str:
        match = re.search(r'"type"\s*:\s*"([^"]+)"', line[:512])
        return match.group(1) if match else ""

    def _is_subagent_session(self, payload: dict) -> bool:
        source = payload.get("source")
        thread_source = str(payload.get("thread_source") or "").strip().casefold()
        return (
            bool(payload.get("parent_thread_id"))
            or isinstance(source, dict)
            or bool(thread_source and thread_source != "user")
        )

    def _project_from_cwd(self, cwd: str) -> dict | None:
        if not cwd:
            return None
        path = Path(cwd)
        return {"name": path.name or "project", "repo_path": cwd}

    def _match_project(self, cwd: str, projects: list[dict]) -> dict | None:
        if not cwd:
            return None
        cwd_key = self._path_key(cwd)
        best: dict | None = None
        best_len = -1
        for project in projects:
            project_key = self._path_key(project["repo_path"])
            if cwd_key == project_key or cwd_key.startswith(project_key + "\\"):
                if len(project_key) > best_len:
                    best = project
                    best_len = len(project_key)
        return best

    def _target_dir(self, summary: CodexSessionSummary, projects: list[dict], output_root: Path, layout: str) -> Path:
        if layout == "project":
            for project in projects:
                if project["name"] == summary.project_name and self._path_key(project["repo_path"]) == self._path_key(summary.project_path):
                    return Path(project["ai_logs_dir"])
        return output_root / self._safe_name(summary.project_name)

    def _to_brief_markdown(self, summary: CodexSessionSummary) -> str:
        user_messages = [message for message in summary.messages if message.role == "user"]
        agent_messages = [message for message in summary.messages if message.role == "codex"]

        lines = [
            f"# Codex session summary - {summary.date}",
            "",
            f"Project: {summary.project_name}",
            f"Dialog topic: {summary.dialog_title}",
            f"Session: {summary.session_id}",
            "",
            "## What the developer asked",
        ]
        for message in user_messages[:12]:
            lines.append(f"- {clip(message.text, 420)}")

        lines.extend(["", "## What happened in the work"])
        for message in agent_messages[:16]:
            lines.append(f"- {clip(message.text, 420)}")

        lines.extend(
            [
                "",
                "## Editorial signal",
                self._editorial_signal(user_messages, agent_messages),
                "",
                "## Safety",
                "This file is a filtered summary of Codex VS Code chat. System/developer instructions, tool dumps and environment blocks are intentionally excluded.",
            ]
        )
        return "\n".join(lines).strip() + "\n"

    def _to_detailed_markdown(self, summary: CodexSessionSummary) -> str:
        lines = [
            f"# История чата Codex — {summary.date}",
            "",
            f"Проект: {summary.project_name}",
            f"Тема диалога: {summary.dialog_title}",
            f"Чат: {summary.session_id}",
        ]
        if summary.git_branch:
            lines.append(f"Ветка Git: {summary.git_branch}")
        lines.extend(
            [
            f"Реплик за день: {len(summary.messages)}",
            "",
            "## Диалог",
            ]
        )
        for message in summary.messages:
            speaker = "Пользователь" if message.role == "user" else "Codex"
            prefix = speaker
            if message.timestamp:
                prefix += f" · {self._local_time(message.timestamp)}"
            lines.extend(["", f"### {prefix}", "", message.text])

        lines.extend(
            [
                "",
                "---",
                "Сохранены все пользовательские и видимые ответы Codex за этот день. "
                "Системные инструкции, внутренние subagent-сессии и tool output не включены; "
                "чувствительные значения замаскированы.",
            ]
        )
        return "\n".join(lines).strip() + "\n"

    def _editorial_signal(self, user_messages: list[CodexMessage], agent_messages: list[CodexMessage]) -> str:
        user_text = " ".join(message.text for message in user_messages)
        agent_text = " ".join(message.text for message in agent_messages[:4])
        signal = f"Главная линия работы: {clip(user_text, 520)}"
        if agent_text:
            signal += f" Направление ответа Codex: {clip(agent_text, 360)}"
        return signal

    def _compress_messages(self, messages: list[CodexMessage]) -> list[CodexMessage]:
        result: list[CodexMessage] = []
        previous_key = None
        previous_timestamp = ""
        for message in messages:
            text = self._drop_noise(message.text)
            if not text:
                continue
            key = (message.role, text)
            if (
                message.role == "user"
                and result
                and result[-1].role == "user"
                and result[-1].text == text
            ):
                # Codex clients may retry the same user turn several seconds
                # apart. Until Codex answers, exact consecutive copies are one
                # logical turn; after an answer the same request is meaningful.
                previous_key = key
                previous_timestamp = message.timestamp
                continue
            if key == previous_key and self._timestamps_are_near(previous_timestamp, message.timestamp):
                previous_timestamp = message.timestamp
                continue
            previous_key = key
            previous_timestamp = message.timestamp
            result.append(CodexMessage(timestamp=message.timestamp, role=message.role, text=text))
        return result

    def _timestamps_are_near(self, first: str, second: str, seconds: float = 2.0) -> bool:
        if not first or not second:
            return first == second
        try:
            first_dt = datetime.fromisoformat(first.replace("Z", "+00:00"))
            second_dt = datetime.fromisoformat(second.replace("Z", "+00:00"))
        except ValueError:
            return first == second
        delta = (second_dt - first_dt).total_seconds()
        return 0 <= delta <= seconds

    def _compress_actions(self, actions: list[CodexAction]) -> list[CodexAction]:
        result: list[CodexAction] = []
        previous_key = None
        for action in actions:
            key = (action.kind, action.text, action.detail)
            if key == previous_key:
                continue
            previous_key = key
            result.append(action)
        return result

    def _parse_response_action(self, timestamp: str, payload: dict, source: str) -> CodexAction | None:
        if payload.get("type") != "function_call":
            return None
        name = payload.get("name") or "tool"
        raw_args = payload.get("arguments") or ""
        text = raw_args
        detail = name
        try:
            args = json.loads(raw_args)
        except json.JSONDecodeError:
            args = {}
        if name == "exec_command" and isinstance(args, dict):
            text = args.get("cmd") or raw_args
            workdir = args.get("workdir")
            if workdir:
                detail = f"{name}, cwd={workdir}"
        elif isinstance(args, dict):
            text = ", ".join(f"{key}={value}" for key, value in args.items() if isinstance(value, (str, int, float, bool))) or raw_args
        text = self._repair_mojibake(str(text))
        text, _ = self.privacy.scan_and_mask(text, source)
        return CodexAction(timestamp=timestamp, kind="command", text=self._normalize_inline(text), detail=detail)

    def _session_overview(self, user_messages: list[CodexMessage], agent_messages: list[CodexMessage]) -> str:
        if not user_messages and not agent_messages:
            return "Сессия импортирована, но содержательных реплик после фильтрации почти не осталось."
        user_text = " ".join(message.text for message in user_messages[:5])
        agent_text = " ".join(message.text for message in agent_messages[-4:])
        overview = f"Главная задача: {clip(user_text, 700)}"
        if agent_text:
            overview += f"\n\nИтоговое направление работы: {clip(agent_text, 700)}"
        return overview

    def _extract_decisions(self, agent_messages: list[CodexMessage]) -> list[str]:
        markers = [
            "сделал",
            "добавил",
            "поправил",
            "проверил",
            "готово",
            "решение",
            "итог",
            "теперь",
            "будет",
            "оставил",
        ]
        decisions: list[str] = []
        for message in agent_messages:
            lowered = message.text.lower()
            if any(marker in lowered for marker in markers):
                decisions.append(clip(message.text, 420))
        return self._unique_strings(decisions)

    def _extract_checks(self, agent_messages: list[CodexMessage], commands: list[CodexAction]) -> list[str]:
        checks: list[str] = []
        command_markers = ["test", "pytest", "npm run", "python -m", "compile", "build", "rg "]
        for action in commands:
            lowered = action.text.lower()
            if any(marker in lowered for marker in command_markers):
                checks.append(f"Команда: {clip(action.text, 220)}")
        for message in agent_messages:
            lowered = message.text.lower()
            if any(word in lowered for word in ["провер", "тест", "ошиб", "проходит", "готово"]):
                checks.append(clip(message.text, 320))
        return self._unique_strings(checks)

    def _extract_open_threads(self, messages: list[CodexMessage]) -> list[str]:
        markers = ["дальше", "осталось", "нужно", "следующий шаг", "не смог", "не удалось", "потом"]
        tails = []
        for message in messages[-20:]:
            lowered = message.text.lower()
            if any(marker in lowered for marker in markers):
                tails.append(clip(message.text, 360))
        return self._unique_strings(tails)

    def _extract_files(self, summary: CodexSessionSummary) -> list[str]:
        text = " ".join([message.text for message in summary.messages] + [action.text for action in summary.actions])
        patterns = [
            r"[\w./\\-]+\.(?:py|js|ts|tsx|jsx|json|md|txt|ps1|env|yml|yaml|toml|css|html|sql)",
            r"(?:^|\s)(?:C:)?[\\/][^\s:]+",
        ]
        files: list[str] = []
        for pattern in patterns:
            for match in re.findall(pattern, text, flags=re.IGNORECASE):
                value = match.strip().strip(".,;:()[]{}'\"")
                if value and len(value) <= 160:
                    files.append(value)
        return self._unique_strings(files)

    def _unique_strings(self, values: list[str]) -> list[str]:
        seen = set()
        result = []
        for value in values:
            clean = self._normalize_inline(value)
            key = clean.casefold()
            if not clean or key in seen:
                continue
            seen.add(key)
            result.append(clean)
        return result

    def _clean_user_message(self, text: str) -> str:
        if "## My request for Codex:" in text:
            text = text.split("## My request for Codex:", 1)[1]
        text = re.sub(r"<environment_context>.*?</environment_context>", " ", text, flags=re.DOTALL)
        text = re.sub(r"<recommended_plugins>.*?</recommended_plugins>", " ", text, flags=re.DOTALL)
        text = re.sub(r"# Context from my IDE setup:.*?## My request for Codex:", " ", text, flags=re.DOTALL)
        return self._normalize_text(text)

    def _clean_agent_message(self, text: str) -> str:
        return self._normalize_text(text)

    def _drop_noise(self, text: str) -> str:
        lowered = text.lower()
        noisy = [
            "<permissions instructions>",
            "<skills_instructions>",
            "<plugins_instructions>",
            "you are codex",
            "filesystem sandboxing",
            "model_context_window",
        ]
        if any(item in lowered for item in noisy):
            return ""
        return text

    def _normalize_text(self, text: str) -> str:
        text = text.replace("\r\n", "\n").replace("\r", "\n")
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text.strip()

    def _normalize_inline(self, text: str) -> str:
        return re.sub(r"\s+", " ", text).strip()

    def _repair_mojibake(self, text: str) -> str:
        if self._mojibake_score(text) < 3:
            return text
        repaired_options = []
        for errors in ["strict", "ignore", "replace"]:
            try:
                repaired_options.append(text.encode("cp1251", errors=errors).decode("utf-8", errors=errors))
            except UnicodeError:
                continue
        if not repaired_options:
            return text
        repaired = max(repaired_options, key=self._cyrillic_score)
        return repaired if self._cyrillic_score(repaired) > self._cyrillic_score(text) else text

    def _mojibake_score(self, text: str) -> int:
        return len(re.findall(r"[РС][\u0080-\u04ff]|вЂ|в„|СЊ|СЃ|Р°|Рё|Рѕ|Рµ", text))

    def _cyrillic_score(self, text: str) -> int:
        return len(re.findall(r"[А-Яа-яЁё]", text))

    def _local_date(self, timestamp: str | float) -> str:
        if isinstance(timestamp, (int, float)):
            return datetime.fromtimestamp(timestamp, tz=self.timezone).date().isoformat()
        clean = timestamp.replace("Z", "+00:00")
        try:
            return datetime.fromisoformat(clean).astimezone(self.timezone).date().isoformat()
        except ValueError:
            return datetime.now(tz=self.timezone).date().isoformat()

    def _local_time(self, timestamp: str) -> str:
        if not timestamp:
            return "time n/a"
        clean = timestamp.replace("Z", "+00:00")
        try:
            return datetime.fromisoformat(clean).astimezone(self.timezone).strftime("%H:%M")
        except ValueError:
            return "time n/a"

    def _safe_name(self, value: str) -> str:
        return re.sub(r"[^\w.-]+", "-", value, flags=re.UNICODE).strip("-") or "project"

    def _topic_slug(self, value: str, limit: int = 72) -> str:
        slug = self._safe_name(value).casefold().strip("._-")
        if len(slug) > limit:
            slug = slug[:limit].rsplit("-", 1)[0].rstrip("._-") or slug[:limit].rstrip("._-")
        return slug or "продолжение-диалога"

    def _path_key(self, path: str) -> str:
        return str(Path(path).resolve()).casefold()

    def _clean_output_root(self, output_root: Path) -> None:
        resolved = output_root.resolve()
        if resolved.anchor == str(resolved):
            raise RuntimeError(f"Refusing to clean unsafe path: {output_root}")
        for path in output_root.rglob("*"):
            if path.is_file() or path.is_symlink():
                path.unlink()
        for path in sorted((item for item in output_root.rglob("*") if item.is_dir()), key=lambda item: len(item.parts), reverse=True):
            path.rmdir()


def timezone_from_offset():
    return timezone(timedelta(hours=3))
