import pytest

import codex_telegram.main as main_module
from codex_telegram.config import ConfigError


def test_run_returns_config_error_exit_code(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    async def _boom() -> None:
        raise ConfigError("Missing required environment variable: TELEGRAM_BOT_TOKEN")

    monkeypatch.setattr(main_module, "_run_async", _boom)

    code = main_module.run()

    captured = capsys.readouterr()
    assert code == 2
    assert "Configuration error" in captured.err
    assert "TELEGRAM_BOT_TOKEN" in captured.err
