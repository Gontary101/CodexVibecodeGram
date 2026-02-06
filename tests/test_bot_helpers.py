from codex_telegram.bot import _sanitize_session_token, _split_args


def test_split_args_respects_quotes() -> None:
    parts = _split_args('resume "session id" "hello world"')
    assert parts == ["resume", "session id", "hello world"]


def test_split_args_falls_back_to_plain_split_on_parse_error() -> None:
    parts = _split_args('resume "unterminated')
    assert parts == ["resume", '"unterminated']


def test_sanitize_session_token_strips_unsafe_chars() -> None:
    assert _sanitize_session_token("  my/session name  ") == "my-session-name"
    assert _sanitize_session_token("***") == "session"
