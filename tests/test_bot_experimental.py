from codex_telegram.bot import _parse_feature_catalog_output


def test_parse_feature_catalog_output_handles_stages_with_spaces() -> None:
    raw = """
undo                             stable             false
runtime_metrics                  under development  false
collab                           experimental       true
"""
    parsed = _parse_feature_catalog_output(raw)

    assert ("undo", "stable", False) in parsed
    assert ("runtime_metrics", "under development", False) in parsed
    assert ("collab", "experimental", True) in parsed


def test_parse_feature_catalog_output_ignores_warnings_and_noise() -> None:
    raw = """
WARNING: proceeding, even though we could not update PATH
not a valid row
shell_tool                       stable             true
"""
    parsed = _parse_feature_catalog_output(raw)

    assert parsed == [("shell_tool", "stable", True)]
