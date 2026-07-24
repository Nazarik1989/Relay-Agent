from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


class NazAiClient:
    def __init__(self, url: str | None = None, api_key: str | None = None, local_path: str | None = None) -> None:
        self.url = url or os.environ.get("NAZAI_API_URL")
        self.api_key = api_key or os.environ.get("NAZAI_API_KEY")
        self.local_path = Path(local_path or os.environ.get("NAZAI_LOCAL_PATH", "")).expanduser()
        if not str(self.local_path).strip():
            sibling = Path("..") / "Naz-AI_Bot"
            self.local_path = sibling if sibling.exists() else Path()

    def is_configured(self) -> bool:
        return bool(self.url or (self.local_path and self.local_path.exists()))

    def edit(self, payload: dict[str, Any]) -> dict[str, Any]:
        if self.local_path and self.local_path.exists():
            return self._edit_local(payload)
        return self._edit_http(payload)

    def _edit_http(self, payload: dict[str, Any]) -> dict[str, Any]:
        if not self.is_configured():
            raise RuntimeError("Нужен NAZAI_API_URL или NAZAI_LOCAL_PATH в .env или переменных окружения.")

        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        request = urllib.request.Request(self.url, data=data, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                body = response.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            error_body = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"Naz_Ai_Bot API error {exc.code}: {error_body}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"Naz_Ai_Bot network error: {exc.reason}") from exc

        try:
            parsed = json.loads(body)
        except json.JSONDecodeError:
            parsed = {"text": body}
        if isinstance(parsed, dict):
            return parsed
        return {"result": parsed}

    def _edit_local(self, payload: dict[str, Any]) -> dict[str, Any]:
        project_path = self.local_path.resolve()
        python_path = self._local_python(project_path)
        with tempfile.TemporaryDirectory(prefix="agent-content-nazai-") as tmp_dir:
            input_path = Path(tmp_dir) / "input.json"
            output_path = Path(tmp_dir) / "output.json"
            runner_path = Path(tmp_dir) / "run_nazai_edit.py"
            input_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            runner_path.write_text(_LOCAL_RUNNER, encoding="utf-8")

            result = subprocess.run(
                [str(python_path), str(runner_path), str(input_path), str(output_path)],
                cwd=project_path,
                text=True,
                encoding="utf-8",
                errors="replace",
                capture_output=True,
                check=False,
                timeout=180,
            )
            if result.returncode != 0:
                raise RuntimeError(
                    "Naz_Ai_Bot local edit failed:\n"
                    f"stdout:\n{result.stdout}\n"
                    f"stderr:\n{result.stderr}"
                )
            if not output_path.exists():
                raise RuntimeError("Naz_Ai_Bot local edit did not create output.json.")
            return json.loads(output_path.read_text(encoding="utf-8"))

    def _local_python(self, project_path: Path) -> Path:
        windows_venv = project_path / ".venv" / "Scripts" / "python.exe"
        posix_venv = project_path / ".venv" / "bin" / "python"
        if windows_venv.exists():
            return windows_venv
        if posix_venv.exists():
            return posix_venv
        return Path(sys.executable)


def nazai_response_to_markdown(response: dict[str, Any], source_path: Path) -> str:
    lines: list[str] = []
    lines.append("# Naz_Ai_Bot редактура")
    lines.append("")
    lines.append(f"Источник: {source_path}")
    lines.append("")

    text = _first_text(response, ["edited", "edited_text", "content", "text", "post", "result"])
    if text:
        lines.append("## Готовый текст")
        lines.append(str(text).strip())
        lines.append("")

    stories = response.get("stories")
    if isinstance(stories, list) and stories:
        lines.append("## Сторис")
        for index, story in enumerate(stories, start=1):
            lines.append("")
            lines.append(f"### {index}")
            if isinstance(story, dict):
                for key, value in story.items():
                    lines.append(f"{key}: {value}")
            else:
                lines.append(str(story))
        lines.append("")

    notes = _first_text(response, ["notes", "comment", "reason", "why"])
    if notes:
        lines.append("## Комментарий редактора")
        lines.append(str(notes).strip())
        lines.append("")

    lines.append("## Raw response")
    lines.append("```json")
    lines.append(json.dumps(response, ensure_ascii=False, indent=2))
    lines.append("```")
    return "\n".join(lines).strip() + "\n"


def _first_text(response: dict[str, Any], keys: list[str]) -> Any:
    for key in keys:
        value = response.get(key)
        if value:
            return value
    return None


_LOCAL_RUNNER = r'''
from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

input_path = Path(sys.argv[1])
output_path = Path(sys.argv[2])
payload = json.loads(input_path.read_text(encoding="utf-8"))
sys.path.insert(0, str(Path.cwd()))

import main  # noqa: E402


async def run() -> None:
    content_pack = payload.get("content_pack") or {}
    best_format = content_pack.get("best_format") or {}
    topic = (
        "Редактура рабочего контента агента: "
        + str(best_format.get("format") or payload.get("date") or "сегодня")
    )
    markdown = str(payload.get("markdown") or "")[:6000]
    instructions = str(payload.get("instructions") or "")
    extra = (
        f"{instructions}\n\n"
        "Исходник от content-agent:\n"
        f"{markdown}\n\n"
        "Верни готовый Telegram-пост в стиле Naz. "
        "Не пересказывай служебные поля. Не упоминай токены, приватные пути и внутренние детали. "
        "Сделай текст пригодным к публикации человеком после быстрой проверки."
    )
    user_id = int(os.getenv("ADMIN_ID", "0") or "0")
    edited = await main.generate_content(user_id, topic, "post", save_generated=True, extra_instruction=extra)
    output_path.write_text(
        json.dumps(
            {
                "edited": edited,
                "source": "nazai-local",
                "model": getattr(main, "CONTENT_MODEL_NAME", ""),
                "comment": "Сгенерировано локальным backend Naz_Ai_Bot через generate_content(...).",
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


asyncio.run(run())
'''
