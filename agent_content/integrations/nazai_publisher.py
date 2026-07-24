from __future__ import annotations

import os
from pathlib import Path

from agent_content.integrations.telegram_sender import TelegramSender
from agent_content.utils import read_env_file


class NazAiPublisher:
    def __init__(self, local_path: str | None = None) -> None:
        raw_path = local_path or os.environ.get("NAZAI_LOCAL_PATH") or str(Path("..") / "Naz-AI_Bot")
        self.local_path = Path(raw_path).expanduser()
        self.env = read_env_file(self.local_path / ".env")
        self.token = self.env.get("BOT_TOKEN") or os.environ.get("NAZAI_BOT_TOKEN")
        self.channel_id = self.env.get("CHANNEL_ID") or os.environ.get("NAZAI_CHANNEL_ID")

    def is_configured(self) -> bool:
        return bool(self.token and self.channel_id)

    def publish_text(self, text: str) -> None:
        if not self.is_configured():
            raise RuntimeError("Нужны BOT_TOKEN и CHANNEL_ID в .env проекта Naz_Ai_Bot или NAZAI_BOT_TOKEN/NAZAI_CHANNEL_ID.")
        TelegramSender(token=self.token, chat_id=self.channel_id).send_text(text)


def extract_publish_text(response: dict) -> str:
    for key in ["edited", "edited_text", "post", "content", "text", "result"]:
        value = response.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return str(response)
