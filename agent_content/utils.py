from __future__ import annotations

import json
import os
from datetime import date
from pathlib import Path
from typing import Any


DEFAULT_CONFIG = {
    "repo_path": ".",
    "projects": [],
    "notes_dir": "content-notes",
    "ai_logs_dir": "ai-logs",
    "terminal_logs_dir": "terminal-logs",
    "outputs_dir": "outputs",
    "language": "ru",
    "story_count": 7,
    "recent_tones": [],
    "project_aliases": {},
    "autopost_times": ["20:00"],
}


def load_config(path: str | None) -> dict[str, Any]:
    load_env_file(Path(".env"))
    config = dict(DEFAULT_CONFIG)
    if path:
        config_path = Path(path)
    else:
        config_path = Path("config.json")

    if config_path.exists():
        user_config = json.loads(config_path.read_text(encoding="utf-8"))
        config.update(user_config)
    return config


def load_env_file(path: Path) -> None:
    values = read_env_file(path)
    for key, value in values.items():
        os.environ.setdefault(key, value)


def read_env_file(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        key = key.strip().lstrip("\ufeff")
        value = value.strip().strip('"').strip("'")
        values[key] = value
    return values


def ensure_dir(path: str | Path) -> Path:
    target = Path(path)
    target.mkdir(parents=True, exist_ok=True)
    return target


def today_iso() -> str:
    return date.today().isoformat()


def configured_projects(config: dict[str, Any], repo_override: str | None = None, notes_override: str | None = None) -> list[dict[str, Any]]:
    if repo_override:
        return [
            {
                "name": Path(repo_override).name or "project",
                "repo_path": repo_override,
                "notes_dir": notes_override or config["notes_dir"],
                "ai_logs_dir": config["ai_logs_dir"],
                "terminal_logs_dir": config["terminal_logs_dir"],
            }
        ]

    projects = config.get("projects") or []
    if projects:
        normalized = []
        for index, project in enumerate(projects, start=1):
            path = project.get("path") or project.get("repo_path") or "."
            normalized.append(
                {
                    "name": project.get("name") or Path(path).name or f"project-{index}",
                    "repo_path": path,
                    "notes_dir": project.get("notes_dir") or config["notes_dir"],
                    "ai_logs_dir": project.get("ai_logs_dir") or config["ai_logs_dir"],
                    "terminal_logs_dir": project.get("terminal_logs_dir") or config["terminal_logs_dir"],
                }
            )
        return normalized

    return [
        {
            "name": Path(config["repo_path"]).name or "project",
            "repo_path": config["repo_path"],
            "notes_dir": notes_override or config["notes_dir"],
            "ai_logs_dir": config["ai_logs_dir"],
            "terminal_logs_dir": config["terminal_logs_dir"],
        }
    ]


def clip(text: str, limit: int) -> str:
    clean = " ".join(text.split())
    if len(clean) <= limit:
        return clean
    return clean[: limit - 1].rstrip() + "..."
