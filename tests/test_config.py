from pathlib import Path

import pytest

from codex_telegram.config import ConfigError, load_settings


def test_load_settings_requires_mandatory_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("OWNER_TELEGRAM_ID", raising=False)

    with pytest.raises(ConfigError):
        load_settings()


def test_load_settings_from_env_file(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text(
        "TELEGRAM_BOT_TOKEN=token\n"
        "OWNER_TELEGRAM_ID=123\n"
        "SQLITE_PATH=./data/test.sqlite3\n"
        "RUNS_DIR=./runs-test\n",
        encoding="utf-8",
    )

    settings = load_settings()

    assert settings.telegram_bot_token == "token"
    assert settings.owner_telegram_id == 123
    assert settings.sqlite_path == (tmp_path / "data/test.sqlite3").resolve()
    assert settings.runs_dir == (tmp_path / "runs-test").resolve()
    assert settings.codex_workdir == tmp_path.resolve()
    assert settings.codex_allowed_workdirs == (tmp_path.resolve(),)
    assert settings.codex_skip_git_repo_check is True
    assert settings.sqlite_path.parent.exists()
    assert settings.runs_dir.exists()


def test_default_session_template_uses_resume(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "token")
    monkeypatch.setenv("OWNER_TELEGRAM_ID", "123")
    monkeypatch.delenv("CODEX_SESSION_CMD_TEMPLATE", raising=False)

    settings = load_settings()

    assert (
        settings.codex_session_cmd_template
        == "codex exec --skip-git-repo-check resume {session_name_quoted} {prompt_quoted}"
    )
