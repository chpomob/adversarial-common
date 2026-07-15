"""Transient provider classification tests."""

import pytest

from adversarial_common.providers import (
    classify_transient_error,
    extract_usage_metadata,
    is_transient_error,
)


@pytest.mark.parametrize(
    ("command", "stderr", "reason"),
    [
        (["codex"], "stream disconnected before completion", "codex_transient"),
        (["claude"], "overloaded_error", "claude_transient"),
        (["pi"], "socket hang up", "pi_transient"),
        (["unknown"], "connection reset by peer", "network"),
        (["codex"], "HTTP 429 rate limited", "http_429"),
    ],
)
def test_provider_specific_transient_signals(command, stderr, reason):
    assert classify_transient_error(command, 1, stderr) == reason


@pytest.mark.parametrize(
    "stderr",
    [
        "HTTP 400 Bad Request",
        "HTTP/1.1 401 Unauthorized",
        "status code: 403",
        "invalid API key",
    ],
)
def test_permanent_client_errors_are_not_transient(stderr):
    assert classify_transient_error(["claude"], 1, stderr) is None


def test_only_fast_124_is_transient():
    assert is_transient_error(
        ["codex"], 124, elapsed=1.0, timeout=10.0
    )
    assert not is_transient_error(
        ["codex"], 124, elapsed=9.5, timeout=10.0
    )


def test_success_is_never_transient_even_with_network_text():
    assert classify_transient_error(
        ["codex"], 0, "connection reset by peer"
    ) is None


def test_claude_native_usage_adapter_handles_model_usage_envelope():
    output = (
        '{"type":"result","modelUsage":{'
        '"claude-sonnet-4":{"inputTokens":11,"outputTokens":7},'
        '"claude-haiku-3.5":{"inputTokens":3,"outputTokens":2}}}'
    )
    assert extract_usage_metadata(output, provider="claude") == {
        "prompt_tokens": 14,
        "completion_tokens": 9,
    }


def test_codex_native_usage_adapter_prefers_last_jsonl_event():
    output = "\n".join([
        '{"type":"turn.started"}',
        '{"type":"turn.completed","usage":{"input_tokens":21,"output_tokens":8}}',
    ])
    assert extract_usage_metadata(output, provider="codex") == {
        "prompt_tokens": 21,
        "completion_tokens": 8,
    }


def test_usage_text_fallback_requires_a_complete_pair():
    assert extract_usage_metadata(
        "input_tokens=13 output_tokens=5", provider="codex"
    ) == {"prompt_tokens": 13, "completion_tokens": 5}
    assert extract_usage_metadata("input_tokens=13", provider="codex") is None

