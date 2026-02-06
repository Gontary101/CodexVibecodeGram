from codex_telegram.models import RiskLevel
from codex_telegram.policy import RiskPolicy


def test_low_risk_prompt() -> None:
    policy = RiskPolicy()
    result = policy.classify_prompt("summarize this file")
    assert result.level == RiskLevel.LOW
    assert result.needs_approval is False


def test_high_risk_prompt_requires_approval() -> None:
    policy = RiskPolicy()
    result = policy.classify_prompt("run rm -rf /tmp/foo")
    assert result.level == RiskLevel.HIGH
    assert result.needs_approval is True
