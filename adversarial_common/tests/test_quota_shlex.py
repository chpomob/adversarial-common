"""Tests for shlex-based command tokenization in _run_checker (plan R11)."""

from pathlib import Path

import pytest

from adversarial_common import ProviderConfig, ProviderEntry, QuotaResolver


def _checker(tmp_path: Path, payload: object) -> Path:
    path = tmp_path / "quota-check"
    path.write_text(
        "#!/usr/bin/env python3\n"
        "import json\n"
        f"print(json.dumps({payload!r}))\n",
        encoding="utf-8",
    )
    path.chmod(0o755)
    return path


def _config(
    *entries: ProviderEntry,
    ttl: int | float = 30,
    role: str = "dev",
) -> ProviderConfig:
    return ProviderConfig(roles={role: entries}, quota_cache_ttl=ttl)


def test_command_with_spaces_produces_concrete_status(tmp_path):
    """AC2: quota_check with spaces yields OK/EXHAUSTED, not UNKNOWN."""
    checker = _checker(
        tmp_path,
        {"results": {"codex": {"session": {"used_pct": 5}}}},
    )
    config = _config(
        ProviderEntry("codex", "echo build", "--cov=src tests/")
    )
    resolver = QuotaResolver(config, checker)

    decision = resolver.resolve("dev", workdir=str(tmp_path))

    assert decision.quota_state == "OK"
    assert decision.quota_state != "UNKNOWN"


def test_single_quoted_argument_not_split(tmp_path):
    """AC3: quotearg tokenized as 2 args, space inside quotes preserved."""
    # ponytail: checker dumps its argv so we can inspect tokenization
    checker = tmp_path / "quota-check"
    checker.write_text(
        "#!/usr/bin/env python3\n"
        "import json, sys\n"
        "print(json.dumps({'results': {'codex': {"
        "'session': {'used_pct': 5}, 'args': sys.argv[1:]"
        "}}}))\n",
        encoding="utf-8",
    )
    checker.chmod(0o755)
    config = _config(ProviderEntry("codex", "echo build", "--name 'my app'"))
    resolver = QuotaResolver(config, checker)

    decision = resolver.resolve("dev", workdir=str(tmp_path))

    codex_data = decision.raw_snapshot.get("codex", {})
    args = codex_data.get("args", [])
    assert args == ["--name", "my app"], (
        f"expected 2 args ['--name', 'my app'], got {args}"
    )


def test_simple_command_unchanged(tmp_path):
    """AC4: existing behavior for commands without spaces is preserved."""
    checker = _checker(
        tmp_path,
        {"results": {"codex": {"session": {"used_pct": 5}}}},
    )
    config = _config(ProviderEntry("codex", "echo build", "--codex"))
    resolver = QuotaResolver(config, checker)

    decision = resolver.resolve("dev", workdir=str(tmp_path))

    assert decision.quota_state == "OK"
    assert decision.alias == "codex"
