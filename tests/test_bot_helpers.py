from types import SimpleNamespace

from codex_telegram.bot import _args, _sanitize_session_token, _split_args


def test_split_args_respects_quotes() -> None:
    parts = _split_args('resume "session id" "hello world"')
    assert parts == ["resume", "session id", "hello world"]


def test_split_args_falls_back_to_plain_split_on_parse_error() -> None:
    parts = _split_args('resume "unterminated')
    assert parts == ["resume", '"unterminated']


def test_sanitize_session_token_strips_unsafe_chars() -> None:
    assert _sanitize_session_token("  my/session name  ") == "my-session-name"
    assert _sanitize_session_token("***") == "session"


def test_args_handles_empty_text_without_crashing() -> None:
    message = SimpleNamespace(text=None)
    assert _args(message) == ""


def test_args_returns_payload_when_present() -> None:
    message = SimpleNamespace(text="/run hello world")
    assert _args(message) == "hello world"
