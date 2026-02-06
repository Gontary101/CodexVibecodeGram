# Poll Skills Guide

This file defines how Codex should use Telegram polls in this project.

## What Polls Are Used For

- Get owner approval for risky jobs.
- Turn multi-choice assistant output into a quick vote.
- Run manual poll smoke tests (`/poll`) and roadmap polls (`/featurepolls`).

## When To Use Polls

Use a poll when:

- A decision has 2 or more valid options and the owner should choose.
- You need explicit approval or rejection before proceeding.
- A completed job needs user prioritization for the next step.

Do not use a poll when:

- There is only one reasonable next step.
- The question is open-ended and needs a free-text answer.
- The action is urgent and should be directly executed/rejected with commands.

## Where Polls Are Triggered

- Risky job approvals: automatically sent as approval UI.
  - If `TELEGRAM_BUSINESS_CONNECTION_ID` is set: checklist UI is attempted first.
  - If checklist is unavailable/fails: fallback to approval poll.
- Assistant follow-up polls: auto-created from successful job summary text when the summary contains a valid multiple-choice pattern.
- Manual operator polls: `/poll [question | option1 | option2 ...]`.
- Curated roadmap polls: `/featurepolls`.

## How Codex Should Format Polls In Responses

Preferred (most reliable): emit an explicit poll block.

```text
[poll]
Question: Which deployment mode should I run?
- Canary
- Blue/Green
[/poll]
```

Alternative (also supported): a question line followed by list options.

```text
Which direction should I take next?
1. Implement API first
2. Write tests first
3. Refactor first
```

## Parser Rules And Limits (Must Respect)

- Include a question ending with `?`.
- Provide at least 2 distinct non-empty options.
- Max 10 options.
- Question is truncated to 300 chars.
- Each option is truncated to 100 chars.
- Duplicate options are removed.
- Supported option prefixes: `-`, `*`, `1.`, `1)`, `A.`, `A)`.
- Auto-generated assistant polls are single-choice (`allows_multiple_answers=false`).

## Decision Quality Rules

When proposing a poll:

- Keep options mutually exclusive.
- Make options concrete and action-oriented.
- Avoid overlapping wording that confuses voting.
- Keep the question about one decision only.

## After Poll Votes

- Approval poll vote should map to approve/reject/revise behavior for the job.
- Assistant poll vote should enqueue a follow-up job using the selected option(s) as context.
- If no valid option is present in an answer, report it and do not guess.

## Quick Checklist Before Sending A Poll

- Is there a real decision to make?
- Are there at least 2 clear options?
- Is the poll format parseable by current notifier logic?
- Is this better than asking for free-text input?
