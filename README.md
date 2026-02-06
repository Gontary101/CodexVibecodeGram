# Codex Telegram Orchestrator

Run Codex tasks from Telegram, track jobs in a queue, and receive results (text + media artifacts) back in chat.

## What This Project Does

- Accepts owner-only Telegram commands
- Queues Codex tasks and executes them on your server
- Adds approval gates for risky prompts
- Persists job state in SQLite
- Sends natural output replies plus non-log artifacts (images/video/docs)
- Supports per-chat active sessions with resume/fork workflows (`codex exec resume ...`)

## Repository Layout

```text
src/codex_telegram/      # application code
systemd/                 # service unit files
tests/                   # test suite
examples/                # optional demo scripts/tasks
.env.example             # environment template
```

## Prerequisites

- Linux server (systemd optional but recommended)
- Python 3.11+
- `codex` CLI installed and authenticated (`codex login`)
- Telegram account + bot token from BotFather

## Quick Start (Local)

1. Create virtualenv and install:

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e .[dev]
```

2. Configure env file:

```bash
cp .env.example .env
```

3. Fill required values in `.env`:

- `TELEGRAM_BOT_TOKEN`
- `OWNER_TELEGRAM_ID`

4. Start bot:

```bash
codex-telegram-bot
```

5. In Telegram, message your bot:

- `/start`
- `/run say hello`

## Getting Telegram Credentials

### 1) `TELEGRAM_BOT_TOKEN`

- Open Telegram -> chat `@BotFather`
- Run `/newbot`
- Copy the token it returns

### 2) `OWNER_TELEGRAM_ID`

Easiest method: message `@userinfobot` and use your numeric user id.

Alternative via Bot API:

- Stop the bot if polling is already running
- Send `/start` to your bot
- Run:

```bash
curl -s "https://api.telegram.org/bot$TELEGRAM_BOT_TOKEN/getUpdates" \
| jq -r '.result // [] | .[] | (.message.from.id // .callback_query.from.id // empty)' | tail -n1
```

## Configuration

See `.env.example` for full list. Most important settings:

- `CODEX_WORKDIR`:
  Codex execution working directory (default `.`)
- `CODEX_ALLOWED_WORKDIRS`:
  Comma-separated allowlist roots used by `/workdir set ...` (default: `CODEX_WORKDIR`)
- `CODEX_SKIP_GIT_REPO_CHECK=true`:
  Injects `--skip-git-repo-check` into `codex exec` commands
- `CODEX_AUTO_SAFE_FLAGS=true`:
  Enables automatic safe runtime flags (like trusted-directory bypass when configured)
- `CODEX_SAFE_DEFAULT_APPROVAL=on-request`:
  Default approval policy when runtime override is not set
- `CODEX_EPHEMERAL_CMD_TEMPLATE`:
  Template for `/run`
- `CODEX_SESSION_CMD_TEMPLATE`:
  Template for `/run_session` (default uses `codex exec resume ...`)
- `TELEGRAM_RESPONSE_MODE=natural|compact|verbose`:
  Controls Telegram output verbosity (default is natural answer-only)

## Telegram Commands

### Task Commands

- `/run <prompt>`
- `/run_session <session_id> <prompt>` (explicit session)
- `/new [name]` (create + activate session for this chat)
- `/resume <session_id_or_name>` (activate/resume for this chat)
- `/fork [source_session]` (create derived session + activate)
- `/session [list|create|stop|use|clear] [name]`
- `/mention <path> <prompt>` (run with file-context hint)
- `/init [extra instructions]` (queue AGENTS.md scaffold task)
- `/review [scope]`
- `/diff [scope]`
- `/plan <task>`
- `/video <job_id>`

### Runtime Control Commands

- `/model [name] [minimal|low|medium|high|xhigh]`
- `/permissions [auto|read-only|full-access|workspace-write|danger-full-access|reset]`
- `/approvals [untrusted|on-failure|on-request|never|reset]`
- `/search [live|cached|disabled|on|off|reset]`
- `/workdir [show|set <path>|reset]`
- `/experimental [list|clear|on <feature>|off <feature>]`
- `/personality [friendly|pragmatic|none|custom <instruction>]`
- `/agent [list|switch <name>|reset]`
- `/status`
- `/compact`

### Queue / Debug Commands

- `/jobs`
- `/job <job_id>` (concise)
- `/info <job_id>` (full diagnostics)
- `/approve <job_id>`
- `/reject <job_id>`
- `/cancel <job_id>`
- `/session [list|create|stop|use|clear] [name]`
- `/mcp [list|get <name>]`
- `/debug-config`

## Run Tests

```bash
pytest -q
```

## Systemd Deployment

1. Copy units:

```bash
sudo cp systemd/codex-telegram-bot.service /etc/systemd/system/
sudo cp systemd/codex-session@.service /etc/systemd/system/
```

2. Ensure `WorkingDirectory` and `EnvironmentFile` match your install path.

3. Enable and start:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now codex-telegram-bot.service
sudo systemctl status codex-telegram-bot.service
```

## Security Notes

- `.env` is gitignored. Never commit secrets.
- If token leaks, revoke/regenerate it in BotFather immediately.
- Keep owner access strict via `OWNER_TELEGRAM_ID`.
- Review `CODEX_*_CMD_TEMPLATE` before production use.

## Troubleshooting

- `Unauthorized` in Telegram:
  wrong `OWNER_TELEGRAM_ID`
- `Not inside a trusted directory`:
  keep `CODEX_SKIP_GIT_REPO_CHECK=true` or use trusted repo dir
- No media attachments:
  ensure outputs are real files, under allowed roots, and extension is in `ALLOWED_ARTIFACT_EXTENSIONS`
