from codex_telegram.bot import _parse_model_payload


def test_parse_model_payload_list_is_help() -> None:
    action, model, reasoning = _parse_model_payload("list")
    assert action == "help"
    assert model is None
    assert reasoning is None


def test_parse_model_payload_help_is_help() -> None:
    action, model, reasoning = _parse_model_payload("help")
    assert action == "help"
    assert model is None
    assert reasoning is None


def test_parse_model_payload_reset_clears_both() -> None:
    action, model, reasoning = _parse_model_payload("reset")
    assert action == "set"
    assert model is None
    assert reasoning == ""


def test_parse_model_payload_model_and_reasoning() -> None:
    action, model, reasoning = _parse_model_payload("gpt-5-codex high")
    assert action == "set"
    assert model == "gpt-5-codex"
    assert reasoning == "high"
