from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Note:
    path: str
    title: str
    text: str


@dataclass
class TerminalLog:
    path: str
    title: str
    text: str


@dataclass
class WorkEvent:
    title: str
    summary: str
    kind: str
    signals: list[str]
    source: str
    score: int = 0
    score_reasons: list[str] = field(default_factory=list)
    tone: str = ""


@dataclass
class PrivacyFinding:
    kind: str
    value: str
    replacement: str
    source: str
