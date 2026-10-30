"""ANTHROPIC_API_KEY loading: whitespace is stripped and warned about, and the
warning never contains the key (FINDINGS.md F11)."""

import logging

import pytest

from agent import config

KEY = "sk-ant-test-0123456789"


@pytest.mark.parametrize("raw", [KEY + "\n", KEY + "\r\n", "  " + KEY, KEY + " "])
def test_surrounding_whitespace_is_stripped_with_a_warning(raw, caplog):
    with caplog.at_level(logging.WARNING, logger="agent.config"):
        assert config.clean_api_key(raw) == KEY
    assert len(caplog.records) == 1
    assert "stripped" in caplog.text
    assert KEY not in caplog.text and "sk-ant" not in caplog.text


def test_clean_key_is_unchanged_and_silent(caplog):
    with caplog.at_level(logging.WARNING, logger="agent.config"):
        assert config.clean_api_key(KEY) == KEY
    assert caplog.records == []


@pytest.mark.parametrize("raw", ["sk-ant-a b", "sk-ant-a\nb"])
def test_inner_whitespace_is_kept_but_warned_about(raw, caplog):
    with caplog.at_level(logging.WARNING, logger="agent.config"):
        assert config.clean_api_key(raw) == raw
    assert "inside it" in caplog.text
    assert "sk-ant" not in caplog.text


@pytest.mark.parametrize("raw,expected", [(None, None), ("", None), ("\n", None)])
def test_missing_or_blank_key_is_none(raw, expected):
    assert config.clean_api_key(raw) is expected
