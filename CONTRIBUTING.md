# Contributing

Thanks for contributing.

## Ground Rules

- Keep changes focused and minimal.
- Prefer correctness and test coverage over broad refactors.
- Do not commit secrets (`.env`, tokens, credentials).
- Preserve existing style and architecture unless a change is clearly justified.

## Local Setup

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e .[dev]
```

## Before Opening a PR

Run:

```bash
pytest -q
python -m compileall -q src tests
```

PRs should include:

- Clear problem statement
- What changed and why
- Any config/env impact
- Test evidence (command + result)

## Code Guidelines

- Python 3.11+
- Keep functions small and explicit.
- Add tests for bug fixes and behavior changes.
- Favor deterministic behavior and clear error messages.
- Avoid introducing breaking command semantics without updating docs.

## Commit Guidance

Recommended commit style:

- `feat: ...`
- `fix: ...`
- `docs: ...`
- `test: ...`
- `refactor: ...`

## Security

If you find a security issue, do not open a public issue with exploit details.
See `SECURITY.md` for disclosure policy.
