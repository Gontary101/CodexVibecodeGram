from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class FeaturePollTemplate:
    question: str
    options: tuple[str, ...]
    allows_multiple_answers: bool = False


FEATURE_ROADMAP_POLLS: tuple[FeaturePollTemplate, ...] = (
    FeaturePollTemplate(
        question="Which planning upgrade would improve your flow the most?",
        options=(
            "Auto-build a step-by-step plan before long jobs",
            "Risk scoring + blockers detected before execution",
            "Smart test selection based on changed files",
            "One-tap plan revisions from Telegram replies",
        ),
    ),
    FeaturePollTemplate(
        question="What should we improve first in approvals UX?",
        options=(
            "Inline diff snippets in approval messages",
            "Preset safety profiles (fast, balanced, strict)",
            "One-click request for safer alternative approach",
            "Reminder nudges for stale pending approvals",
        ),
    ),
    FeaturePollTemplate(
        question="Which collaboration feature should we prioritize?",
        options=(
            "Shared roadmap board in Telegram",
            "Threaded follow-ups per job for context continuity",
            "Daily digest of completed work and blockers",
            "Reusable slash-command macros for common tasks",
        ),
    ),
    FeaturePollTemplate(
        question="Which artifact experience upgrade is most valuable?",
        options=(
            "Gallery-style previews for generated artifacts",
            "Auto-generated executive summary with each run",
            "Download all artifacts for a job as one ZIP",
            "Before/after comparison cards for new outputs",
        ),
    ),
)
