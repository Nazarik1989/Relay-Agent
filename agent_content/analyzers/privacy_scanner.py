from __future__ import annotations

import re

from agent_content.config.privacy_rules import PRIVACY_PATTERNS, SENSITIVE_KEYWORDS
from agent_content.models import PrivacyFinding


class PrivacyScanner:
    def scan_and_mask(self, text: str, source: str) -> tuple[str, list[PrivacyFinding]]:
        findings: list[PrivacyFinding] = []
        masked = text

        for rule in PRIVACY_PATTERNS:
            pattern = re.compile(rule["pattern"])
            for match in pattern.finditer(masked):
                findings.append(
                    PrivacyFinding(
                        kind=rule["kind"],
                        value=match.group(0),
                        replacement=rule["replacement"],
                        source=source,
                    )
                )
            masked = pattern.sub(rule["replacement"], masked)

        lowered = masked.lower()
        for keyword in SENSITIVE_KEYWORDS:
            if keyword.lower() in lowered:
                findings.append(
                    PrivacyFinding(
                        kind="sensitive_keyword",
                        value=keyword,
                        replacement="[требует ручной проверки]",
                        source=source,
                    )
                )

        return masked, findings

    def mask_dict(self, payload: dict, source: str) -> tuple[dict, list[PrivacyFinding]]:
        findings: list[PrivacyFinding] = []
        masked = {}
        for key, value in payload.items():
            if isinstance(value, str):
                masked_value, found = self.scan_and_mask(value, f"{source}.{key}")
                masked[key] = masked_value
                findings.extend(found)
            elif isinstance(value, list):
                masked_list = []
                for index, item in enumerate(value):
                    if isinstance(item, str):
                        masked_item, found = self.scan_and_mask(item, f"{source}.{key}[{index}]")
                        masked_list.append(masked_item)
                        findings.extend(found)
                    elif isinstance(item, dict):
                        masked_item, found = self.mask_dict(item, f"{source}.{key}[{index}]")
                        masked_list.append(masked_item)
                        findings.extend(found)
                    else:
                        masked_list.append(item)
                masked[key] = masked_list
            else:
                masked[key] = value
        return masked, findings
