from __future__ import annotations

import json
from pathlib import Path


class JsonWriter:
    def write(self, path: Path, payload: dict) -> Path:
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return path
