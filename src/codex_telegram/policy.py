from __future__ import annotations

import re
from dataclasses import dataclass

from .models import RiskLevel


@dataclass(slots=True)
class RiskDecision:
    level: RiskLevel
    needs_approval: bool
    reason: str


class RiskPolicy:
    def __init__(self) -> None:
        self._high_patterns = [
            re.compile(p, re.IGNORECASE)
            for p in (
                r"\brm\s+-rf\b",
                r"\bmkfs\b",
                r"\bdd\s+if=",
                r"\bshutdown\b",
                r"\breboot\b",
                r"\buserdel\b",
                r"\bchown\s+-R\s+/",
                r"\bchmod\s+777\s+/",
                r"\b:(){:|:&};:\b",
            )
        ]
        self._medium_patterns = [
            re.compile(p, re.IGNORECASE)
            for p in (
                r"\bsudo\b",
                r"\brm\b",
                r"\bgit\s+push\b",
                r"\bdocker\s+(run|compose|rm|rmi|exec)\b",
                r"\bsystemctl\b",
                r"\bapt(-get)?\s+",
                r"\byum\s+",
                r"\bpacman\s+",
                r"\bpip\s+install\b",
                r"\bnpm\s+install\b",
                r"\bcargo\s+install\b",
                r"\bkubectl\s+",
            )
        ]

    def classify_prompt(self, prompt: str) -> RiskDecision:
        normalized = prompt.strip()
        if not normalized:
            return RiskDecision(RiskLevel.LOW, False, "empty prompt")

        for pattern in self._high_patterns:
            if pattern.search(normalized):
                return RiskDecision(
                    level=RiskLevel.HIGH,
                    needs_approval=True,
                    reason=f"matches high-risk pattern: {pattern.pattern}",
                )

        for pattern in self._medium_patterns:
            if pattern.search(normalized):
                return RiskDecision(
                    level=RiskLevel.MEDIUM,
                    needs_approval=True,
                    reason=f"matches medium-risk pattern: {pattern.pattern}",
                )

        return RiskDecision(RiskLevel.LOW, False, "no risky patterns detected")
