"""Transient provider classification tests."""

import pytest

from adversarial_common.providers import (
    classify_transient_error,
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

