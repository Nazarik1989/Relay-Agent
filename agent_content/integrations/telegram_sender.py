from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from pathlib import Path


class TelegramSender:
    def __init__(self, token: str | None = None, chat_id: str | None = None) -> None:
        self.token = token or os.environ.get("TELEGRAM_BOT_TOKEN")
        self.chat_id = chat_id or os.environ.get("TELEGRAM_CHAT_ID")

    def is_configured(self) -> bool:
        return bool(self.token and self.chat_id)

    def send_text(self, text: str) -> None:
        self._require_config()
        for chunk in self._chunks(text, 3900):
            self._post("sendMessage", {"chat_id": self.chat_id, "text": chunk})

    def send_file(self, path: Path, caption: str | None = None) -> None:
        self._require_config()
        boundary = "----agent-content-boundary"
        body = self._multipart_body(path, boundary, caption)
        request = urllib.request.Request(
            f"https://api.telegram.org/bot{self.token}/sendDocument",
            data=body,
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
            method="POST",
        )
        self._open(request)

    def _post(self, method: str, payload: dict) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            f"https://api.telegram.org/bot{self.token}/{method}",
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        self._open(request)

    def _open(self, request: urllib.request.Request) -> None:
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                body = response.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            error_body = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"Telegram API error {exc.code}: {error_body}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"Telegram network error: {exc.reason}") from exc

        payload = json.loads(body)
        if not payload.get("ok"):
            raise RuntimeError(f"Telegram API rejected request: {body}")

    def _require_config(self) -> None:
        if not self.is_configured():
            raise RuntimeError("Нужны TELEGRAM_BOT_TOKEN и TELEGRAM_CHAT_ID в .env или переменных окружения.")

    def _multipart_body(self, path: Path, boundary: str, caption: str | None) -> bytes:
        file_bytes = path.read_bytes()
        parts: list[bytes] = []
        parts.append(self._field(boundary, "chat_id", str(self.chat_id)))
        if caption:
            parts.append(self._field(boundary, "caption", caption))
        header = (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="document"; filename="{path.name}"\r\n'
            "Content-Type: text/markdown\r\n\r\n"
        ).encode("utf-8")
        parts.append(header + file_bytes + b"\r\n")
        parts.append(f"--{boundary}--\r\n".encode("utf-8"))
        return b"".join(parts)

    def _field(self, boundary: str, name: str, value: str) -> bytes:
        return (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="{name}"\r\n\r\n'
            f"{value}\r\n"
        ).encode("utf-8")

    def _chunks(self, text: str, size: int) -> list[str]:
        if len(text) <= size:
            return [text]
        chunks = []
        current = ""
        for line in text.splitlines(keepends=True):
            if len(current) + len(line) > size:
                chunks.append(current)
                current = ""
            current += line
        if current:
            chunks.append(current)
        return chunks
