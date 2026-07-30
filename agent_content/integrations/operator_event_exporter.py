from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from shutil import rmtree
from typing import Iterable
from uuid import uuid4

from agent_content.analyzers.privacy_scanner import PrivacyScanner
from agent_content.integrations.codex_story_exporter import StoryDocument
from agent_content.integrations.codex_topic_exporter import TopicDocument, TopicSourceMessage


@dataclass(frozen=True)
class OperatorEventDocument:
    relative_path: Path
    payload: dict[str, object]


@dataclass(frozen=True)
class _VisibleMessage:
    timestamp: str
    role: str
    text: str
    source_ref: str
    is_topic_identity: bool
    finding_kinds: frozenset[str]

    @property
    def privacy_blocked(self) -> bool:
        return "sensitive_keyword" in self.finding_kinds


class OperatorEventExporter:
    """Build privacy-safe deterministic OperatorEvent sidecars.

    This exporter consumes the already partitioned topic/story documents.  It
    never scans Codex sessions itself and never uses an LLM.  Only explicitly
    labelled visible statements may populate causal, evidence, and result
    fields; unknown values remain null and fail closed for later Reel use.
    """

    CONTRACT_VERSION = "operator-event-set.v1"
    EVENT_TYPE = "work_event"
    MAX_VALUE_CHARS = 480
    MAX_LIST_ITEMS = 8

    _DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
    _TOPIC_ID_RE = re.compile(r"^[0-9a-f]{12}$")
    _SOURCE_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
    _UUID_RE = re.compile(
        r"(?i)\b[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\b"
    )
    _TIMESTAMP_RE = re.compile(
        r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2}(?:\.\d{1,6})?)?(?:Z|[+-]\d{2}:\d{2})?$"
    )
    _BULLET_RE = re.compile(r"^\s*(?:[-*+]\s+|\d+[.)]\s+)")

    _FACT_LABELS = {
        "initial_state": ("initial state", "исходное состояние"),
        "trigger": ("trigger", "триггер"),
        "initial_assumption": (
            "initial assumption",
            "исходное предположение",
            "первоначальное предположение",
        ),
        "actual_cause": (
            "actual cause",
            "root cause",
            "фактическая причина",
            "подтверждённая причина",
            "подтвержденная причина",
        ),
        "change": ("change", "изменение"),
        "technical_result": ("technical result", "технический результат"),
    }
    _EVIDENCE_LABELS = (
        "evidence",
        "proof",
        "подтверждение",
        "доказательство",
    )
    _COMMENTARY_LABELS = {
        "human_consequence": ("human consequence", "последствие для человека"),
        "lesson": ("lesson", "урок", "вывод"),
        "open_questions": ("open question", "открытый вопрос"),
    }

    def __init__(self, output_root: str | Path, privacy: PrivacyScanner | None = None) -> None:
        self.output_root = Path(output_root)
        self.privacy = privacy or PrivacyScanner()

    def build_documents(
        self,
        topics: Iterable[TopicDocument],
        stories: Iterable[StoryDocument],
    ) -> list[OperatorEventDocument]:
        topic_by_id: dict[str, TopicDocument] = {}
        for topic in topics:
            if topic.topic_id in topic_by_id:
                raise RuntimeError(f"Duplicate topic ID for OperatorEvent export: {topic.topic_id}")
            topic_by_id[topic.topic_id] = topic

        documents: list[OperatorEventDocument] = []
        seen_paths: set[Path] = set()
        for story in sorted(stories, key=lambda item: item.relative_path.as_posix().casefold()):
            topic = topic_by_id.get(story.topic_id)
            if topic is None:
                raise RuntimeError(f"Unknown topic for OperatorEvent export: {story.topic_id}")
            self._validate_pair(topic, story)
            project = story.relative_path.parts[0]
            relative_path = Path(project) / story.date / f"t-{story.topic_id}.json"
            self._validate_relative_path(relative_path)
            if relative_path in seen_paths:
                raise RuntimeError(f"OperatorEvent path collision: {relative_path.as_posix()}")
            seen_paths.add(relative_path)
            documents.append(
                OperatorEventDocument(
                    relative_path=relative_path,
                    payload=self._build_payload(topic, story, project),
                )
            )
        return documents

    def write_documents(self, documents: Iterable[OperatorEventDocument]) -> dict[str, object]:
        source = sorted(documents, key=lambda item: item.relative_path.as_posix().casefold())
        if not source:
            raise ValueError("No OperatorEvent documents to export")
        if len({item.relative_path for item in source}) != len(source):
            raise RuntimeError("Duplicate OperatorEvent document path")

        encoded: list[tuple[Path, bytes]] = []
        for document in source:
            self._validate_relative_path(document.relative_path)
            raw = (
                json.dumps(document.payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
            ).encode("utf-8")
            if b"\x00" in raw:
                raise ValueError(f"NUL byte in OperatorEvent document: {document.relative_path}")
            encoded.append((document.relative_path, raw))

        resolved = self.output_root.resolve()
        if resolved == Path(resolved.anchor):
            raise RuntimeError(f"Refusing unsafe OperatorEvent root: {self.output_root}")
        parent = self.output_root.parent
        parent.mkdir(parents=True, exist_ok=True)
        staging = parent / f".{self.output_root.name}.staging-{uuid4().hex}"
        backup = parent / f".{self.output_root.name}.backup-{uuid4().hex}"
        staging.mkdir(parents=False, exist_ok=False)

        try:
            for relative_path, raw in encoded:
                target = staging / relative_path
                target.parent.mkdir(parents=True, exist_ok=True)
                if target.exists():
                    raise RuntimeError(f"Duplicate OperatorEvent target: {relative_path.as_posix()}")
                target.write_bytes(raw)

            if self.output_root.exists():
                self.output_root.replace(backup)
            staging.replace(self.output_root)
            if backup.exists():
                rmtree(backup)
        except Exception:
            if staging.exists():
                rmtree(staging)
            if backup.exists() and not self.output_root.exists():
                backup.replace(self.output_root)
            raise

        dates = {item.relative_path.parts[1] for item in source}
        projects = {item.relative_path.parts[0] for item in source}
        return {
            "operator_events_dir": self.output_root,
            "document_count": len(source),
            "date_count": len(dates),
            "project_count": len(projects),
            "documents": [self.output_root / item.relative_path for item in source],
        }

    def export(
        self,
        topics: Iterable[TopicDocument],
        stories: Iterable[StoryDocument],
    ) -> dict[str, object]:
        return self.write_documents(self.build_documents(topics, stories))

    def _validate_pair(self, topic: TopicDocument, story: StoryDocument) -> None:
        expected_hash = hashlib.sha256(topic.text.encode("utf-8")).hexdigest()
        if story.source_hash != expected_hash:
            raise RuntimeError(f"Story source hash mismatch for topic {story.topic_id}")
        if (
            story.topic_id != topic.topic_id
            or story.project_name != topic.project_name
            or story.date != topic.date
        ):
            raise RuntimeError(f"Story/topic metadata mismatch for topic {story.topic_id}")
        if story.relative_path != topic.relative_path:
            raise RuntimeError(f"Story/topic path mismatch for topic {story.topic_id}")
        if len(story.relative_path.parts) < 3:
            raise ValueError(f"Unsafe Story path for OperatorEvent export: {story.relative_path}")
        if self._UUID_RE.search(story.relative_path.as_posix()):
            raise ValueError(f"Private identifier in Story path for topic {story.topic_id}")
        if story.relative_path.parts[1] != story.date:
            raise ValueError(f"Story date/path mismatch for topic {story.topic_id}")
        project = story.relative_path.parts[0]
        if project != self._safe_project_name(story.project_name):
            raise ValueError(f"Story project/path mismatch for topic {story.topic_id}")
        masked_project, project_findings = self.privacy.scan_and_mask(
            project,
            f"operator-event:{story.topic_id}:project",
        )
        if masked_project != project or project_findings:
            raise ValueError(f"Private project metadata for topic {story.topic_id}")
        if not self._DATE_RE.fullmatch(story.date):
            raise ValueError(f"Unsafe OperatorEvent date: {story.date}")
        if not self._TOPIC_ID_RE.fullmatch(story.topic_id):
            raise ValueError(f"Unsafe OperatorEvent topic ID: {story.topic_id}")
        if not self._SOURCE_HASH_RE.fullmatch(story.source_hash):
            raise ValueError(f"Unsafe OperatorEvent source hash: {story.source_hash}")

    def _validate_relative_path(self, relative_path: Path) -> None:
        path = Path(relative_path)
        if path.is_absolute() or len(path.parts) != 3:
            raise ValueError(f"Unsafe OperatorEvent document path: {path}")
        if any(part in {"", ".", ".."} for part in path.parts):
            raise ValueError(f"Unsafe OperatorEvent document path: {path}")
        project, date, filename = path.parts
        if project.strip(" .") != project or not project:
            raise ValueError(f"Unsafe OperatorEvent project path: {project}")
        if not self._DATE_RE.fullmatch(date):
            raise ValueError(f"Unsafe OperatorEvent date path: {date}")
        if not re.fullmatch(r"t-[0-9a-f]{12}\.json", filename):
            raise ValueError(f"Unsafe OperatorEvent filename: {filename}")

    def _build_payload(
        self,
        topic: TopicDocument,
        story: StoryDocument,
        project: str,
    ) -> dict[str, object]:
        session_ref = self._session_ref(topic.session_id)
        messages = self._visible_messages(topic, session_ref)
        source_refs = self._unique(item.source_ref for item in messages)
        reason_codes: list[str] = []

        finding_kinds = {kind for item in messages for kind in item.finding_kinds}
        raw_summary = self._normalize_value(topic.title)
        masked_summary = self._mask_private_identifiers(raw_summary, topic.session_id)
        summary_value, summary_findings = self.privacy.scan_and_mask(
            masked_summary,
            f"operator-event:{topic.topic_id}:event_summary",
        )
        summary_value = self._clip(self._normalize_value(summary_value))
        summary_kinds = {item.kind for item in summary_findings}
        if masked_summary != raw_summary:
            summary_kinds.add("private_identifier")
        finding_kinds.update(summary_kinds)
        normalized_summary = self._normalize_value(summary_value).casefold()
        summary_sources = [
            item
            for item in messages
            if normalized_summary
            and normalized_summary in self._normalize_value(item.text).casefold()
        ]
        summary_source = next(
            (item for item in summary_sources if item.is_topic_identity),
            summary_sources[0] if summary_sources else None,
        )
        if not summary_value:
            reason_codes.append("event_summary_missing")
        if "sensitive_keyword" in summary_kinds:
            summary_value = ""
            reason_codes.append("event_summary_privacy_blocked")
        elif summary_value and summary_source is None:
            summary_value = ""
            reason_codes.append("event_summary_unconfirmed")

        facts: dict[str, object] = {
            "event_summary": self._fact(
                summary_value or None,
                [summary_source.source_ref] if summary_value and summary_source else [],
            )
        }
        for field, labels in self._FACT_LABELS.items():
            value, refs, field_reasons = self._extract_scalar(messages, labels, field)
            facts[field] = self._fact(value, refs)
            reason_codes.extend(field_reasons)

        evidence, evidence_reasons = self._extract_evidence(messages)
        facts["evidence"] = evidence
        reason_codes.extend(evidence_reasons)

        commentary: dict[str, object] = {}
        for field, labels in self._COMMENTARY_LABELS.items():
            if field == "open_questions":
                values, field_reasons = self._extract_commentary_list(messages, labels, field)
                commentary[field] = values
            else:
                value, _, field_reasons = self._extract_scalar(messages, labels, field)
                commentary[field] = value
            reason_codes.extend(field_reasons)

        if facts["actual_cause"]["value"] is None:  # type: ignore[index]
            reason_codes.append("actual_cause_unconfirmed")
        if not evidence:
            reason_codes.append("evidence_unconfirmed")
        if facts["technical_result"]["value"] is None:  # type: ignore[index]
            reason_codes.append("technical_result_unconfirmed")
        if topic.boundary_reason == "safety_chunk":
            reason_codes.append("ambiguous_event_boundary")
        if topic.cancelled:
            reason_codes.append("topic_cancelled")
        elif not topic.closed:
            reason_codes.append("topic_open")
        if not story.publishable:
            reason_codes.append("source_story_not_publishable")
        if "sensitive_keyword" in finding_kinds:
            reason_codes.append("privacy_sensitive_source")
        elif finding_kinds:
            reason_codes.append("privacy_data_masked")

        if "sensitive_keyword" in finding_kinds:
            privacy_status = "needs_review"
        elif finding_kinds:
            privacy_status = "masked"
        else:
            privacy_status = "clear"

        occurred_at = self._occurred_at(messages, reason_codes)
        reason_codes = sorted(set(reason_codes))
        event_id = self._event_id(project, story)
        event = {
            "event_id": event_id,
            "event_type": self.EVENT_TYPE,
            "occurred_at": occurred_at,
            "source_session_refs": [session_ref],
            "source_message_refs": source_refs,
            "event_facts": facts,
            "operator_commentary": commentary,
            "publication_copy_ref": None,
            "privacy_status": privacy_status,
            "content_status": "needs_review" if reason_codes else "ready",
            "reason_codes": reason_codes,
        }
        return {
            "contract_version": self.CONTRACT_VERSION,
            "project": project,
            "date": story.date,
            "topic_id": story.topic_id,
            "source_hash": story.source_hash,
            "events": [event],
        }

    def _visible_messages(
        self,
        topic: TopicDocument,
        session_ref: str,
    ) -> list[_VisibleMessage]:
        result: list[_VisibleMessage] = []
        for message in topic.source_messages:
            raw_text = self._normalize_visible_text(message.text)
            role = self._normalize_role(message.role)
            if role not in {"user", "codex"}:
                continue
            timestamp = str(message.timestamp or "").strip()
            identity_text = re.sub(r"\s+", " ", raw_text).strip()
            identity_seed = "\0".join((topic.session_id, timestamp, role, identity_text))
            is_topic_identity = (
                hashlib.sha256(identity_seed.encode("utf-8")).hexdigest()[:12] == topic.topic_id
            )
            private_safe_text = self._mask_private_identifiers(raw_text, topic.session_id)
            safe_text, findings = self.privacy.scan_and_mask(
                private_safe_text,
                f"operator-event:{topic.topic_id}:message",
            )
            safe_text = self._normalize_visible_text(safe_text)
            finding_kinds = {item.kind for item in findings}
            if private_safe_text != raw_text:
                finding_kinds.add("private_identifier")
            message_seed = "\0".join(
                (
                    "operator-event-message.v1",
                    session_ref,
                    timestamp,
                    role,
                    safe_text,
                )
            )
            result.append(
                _VisibleMessage(
                    timestamp=timestamp,
                    role=role,
                    text=safe_text,
                    source_ref="message-"
                    + hashlib.sha256(message_seed.encode("utf-8")).hexdigest()[:24],
                    is_topic_identity=is_topic_identity,
                    finding_kinds=frozenset(finding_kinds),
                )
            )
        return result

    def _extract_scalar(
        self,
        messages: list[_VisibleMessage],
        labels: tuple[str, ...],
        field: str,
    ) -> tuple[str | None, list[str], list[str]]:
        candidates = self._labelled_candidates(messages, labels)
        if not candidates:
            return None, [], []
        safe = [item for item in candidates if not item[2]]
        blocked = len(safe) != len(candidates)
        by_value: dict[str, tuple[str, list[str]]] = {}
        for value, ref, _ in safe:
            key = value.casefold()
            current = by_value.get(key)
            if current is None:
                by_value[key] = (value, [ref])
            elif ref not in current[1]:
                current[1].append(ref)
        reasons: list[str] = []
        if blocked:
            reasons.append(f"{field}_privacy_blocked")
        if len(by_value) != 1:
            if len(by_value) > 1:
                reasons.append(f"{field}_ambiguous")
            return None, [], reasons
        value, refs = next(iter(by_value.values()))
        return value, refs, reasons

    def _extract_evidence(
        self,
        messages: list[_VisibleMessage],
    ) -> tuple[list[dict[str, object]], list[str]]:
        candidates = self._labelled_candidates(messages, self._EVIDENCE_LABELS)
        evidence: list[dict[str, object]] = []
        reasons: list[str] = []
        seen: set[str] = set()
        for value, ref, blocked in candidates:
            if blocked:
                reasons.append("evidence_privacy_blocked")
                continue
            key = value.casefold()
            if key in seen:
                continue
            seen.add(key)
            evidence.append(self._fact(value, [ref]))
        if len(evidence) > self.MAX_LIST_ITEMS:
            reasons.append("evidence_truncated")
            evidence = evidence[: self.MAX_LIST_ITEMS]
        return evidence, reasons

    def _extract_commentary_list(
        self,
        messages: list[_VisibleMessage],
        labels: tuple[str, ...],
        field: str,
    ) -> tuple[list[str], list[str]]:
        candidates = self._labelled_candidates(messages, labels)
        values: list[str] = []
        reasons: list[str] = []
        seen: set[str] = set()
        for value, _, blocked in candidates:
            if blocked:
                reasons.append(f"{field}_privacy_blocked")
                continue
            key = value.casefold()
            if key not in seen:
                seen.add(key)
                values.append(value)
        if len(values) > self.MAX_LIST_ITEMS:
            reasons.append(f"{field}_truncated")
            values = values[: self.MAX_LIST_ITEMS]
        return values, reasons

    def _labelled_candidates(
        self,
        messages: list[_VisibleMessage],
        labels: tuple[str, ...],
    ) -> list[tuple[str, str, bool]]:
        result: list[tuple[str, str, bool]] = []
        for message in messages:
            for raw_line in message.text.splitlines():
                line = self._BULLET_RE.sub("", raw_line.strip()).strip()
                line = line.replace("**", "").replace("__", "")
                for label in labels:
                    match = re.match(
                        rf"^{re.escape(label)}\s*[:=\-]\s*(.+)$",
                        line,
                        flags=re.IGNORECASE,
                    )
                    if not match:
                        continue
                    value = self._clip(self._normalize_value(match.group(1)))
                    if value:
                        result.append((value, message.source_ref, message.privacy_blocked))
                    break
        return result

    def _occurred_at(self, messages: list[_VisibleMessage], reason_codes: list[str]) -> str | None:
        source = next((item for item in messages if item.is_topic_identity), None)
        if source is None and messages:
            source = messages[0]
        if source is None or not source.timestamp:
            reason_codes.append("occurred_at_missing")
            return None
        if not self._TIMESTAMP_RE.fullmatch(source.timestamp):
            reason_codes.append("occurred_at_invalid")
            return None
        return source.timestamp

    def _event_id(self, project: str, story: StoryDocument) -> str:
        seed = "\0".join(
            (
                self.CONTRACT_VERSION,
                project,
                story.date,
                story.topic_id,
                story.source_hash,
            )
        )
        return "oev-" + hashlib.sha256(seed.encode("utf-8")).hexdigest()[:24]

    @staticmethod
    def _session_ref(session_id: str) -> str:
        seed = "operator-event-session.v1\0" + str(session_id)
        return "session-" + hashlib.sha256(seed.encode("utf-8")).hexdigest()[:24]

    @staticmethod
    def _fact(value: str | None, refs: Iterable[str]) -> dict[str, object]:
        return {"value": value, "source_message_refs": list(refs) if value is not None else []}

    @staticmethod
    def _normalize_role(value: str) -> str:
        return re.sub(r"\s+", " ", str(value or "")).strip().casefold()

    @staticmethod
    def _normalize_visible_text(value: str) -> str:
        text = str(value or "").replace("\r\n", "\n").replace("\r", "\n")
        text = re.sub(r"[ \t]+", " ", text)
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text.strip()

    @staticmethod
    def _normalize_value(value: str) -> str:
        return re.sub(r"\s+", " ", str(value or "")).strip()

    def _mask_private_identifiers(self, value: str, session_id: str) -> str:
        masked = str(value or "")
        raw_session_id = str(session_id or "").strip()
        if raw_session_id:
            masked = masked.replace(raw_session_id, "[session identifier hidden]")
        return self._UUID_RE.sub("[session identifier hidden]", masked)

    @staticmethod
    def _safe_project_name(value: str) -> str:
        normalized = unicodedata.normalize("NFC", re.sub(r"\s+", " ", str(value or "")).strip())
        normalized = re.sub(r"[^\w.-]+", "-", normalized, flags=re.UNICODE)
        normalized = re.sub(r"-{2,}", "-", normalized).strip(" .-")
        if len(normalized) > 80:
            normalized = normalized[:80].rstrip(" .-")
        return normalized or "project"

    def _clip(self, value: str) -> str:
        if len(value) <= self.MAX_VALUE_CHARS:
            return value
        return value[: self.MAX_VALUE_CHARS - 1].rstrip() + "…"

    @staticmethod
    def _unique(values: Iterable[str]) -> list[str]:
        result: list[str] = []
        seen: set[str] = set()
        for value in values:
            if value not in seen:
                seen.add(value)
                result.append(value)
        return result
