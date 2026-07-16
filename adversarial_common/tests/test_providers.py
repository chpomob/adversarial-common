"""Provider registry and transient classification tests."""

import json
from pathlib import Path

import pytest
import yaml

from adversarial_common.providers import (
    ProviderConfig,
    ProviderConfigError,
    ProviderEntry,
    classify_transient_error,
    extract_usage_metadata,
    is_transient_error,
    load_provider_config,
    resolve_provider_config_path,
    resolve_role_cmd,
)


def _write_config(path: Path, **updates) -> Path:
    config = {
        "dev": [
            {"alias": "dev-a", "cmd": "echo dev-a", "stop_threshold": 90},
            {"alias": "dev-b", "cmd": "echo dev-b"},
        ],
        "review": [
            {"alias": "review-a", "cmd": "echo review-a"},
            {"alias": "review-b", "cmd": "echo review-b"},
        ],
        "quota_cmd": "quota-check --json",
        "quota_cache_ttl": 12,
    }
    config.update(updates)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")
    return path


def test_provider_config_is_immutable_and_round_trips(tmp_path):
    original = ProviderConfig(
        roles={
            "dev": (
                ProviderEntry("dev-a", "echo a", "--a", 80),
                ProviderEntry("dev-b", "echo b"),
            ),
            "review": (
                ProviderEntry("review-a", "echo c"),
                ProviderEntry("review-b", "echo d", stop_threshold=2.0),
            ),
        },
        quota_cmd="quota --json",
        quota_cache_ttl=45,
    )
    path = tmp_path / "providers.yaml"
    path.write_text(yaml.safe_dump(original.to_dict(), sort_keys=False))

    loaded = ProviderConfig.load(path, environ={})

    assert loaded is not None
    assert loaded.roles == original.roles
    assert loaded.quota_cmd == original.quota_cmd
    assert loaded.quota_cache_ttl == original.quota_cache_ttl
    assert loaded["dev"][0].command == "echo a"
    assert loaded["dev"][0].cmd == "echo a"
    with pytest.raises(TypeError):
        loaded.roles["dev"] = ()


def test_path_precedence_and_default_home(monkeypatch, tmp_path):
    home = tmp_path / "home"
    default = _write_config(home / ".config/adversarial/providers.yaml")
    env_path = _write_config(
        tmp_path / "env.yaml",
        dev=[{"alias": "from-env", "cmd": "echo env"}],
    )
    cli_path = _write_config(
        tmp_path / "cli.yaml",
        dev=[{"alias": "from-cli", "cmd": "echo cli"}],
    )
    monkeypatch.setenv("HOME", str(home))

    from_default = load_provider_config(environ={"HOME": str(home)})
    # expanduser reads the process HOME, while environ controls loader variables.
    assert from_default is not None
    assert from_default.source_path == default.resolve()

    from_env = load_provider_config(
        environ={"ADVERSARIAL_PROVIDER_CONFIG": str(env_path)}
    )
    assert from_env is not None
    assert from_env["dev"][0].alias == "from-env"

    from_cli = load_provider_config(
        cli_path,
        environ={"ADVERSARIAL_PROVIDER_CONFIG": str(env_path)},
    )
    assert from_cli is not None
    assert from_cli["dev"][0].alias == "from-cli"


def test_missing_default_is_legacy_but_selected_missing_path_fails(
    monkeypatch, tmp_path
):
    home = str(tmp_path / "empty-home")
    monkeypatch.setenv("HOME", home)
    assert load_provider_config(environ={"HOME": home}) is None

    with pytest.raises(ProviderConfigError, match="PROVIDER_CONFIG_NOT_FOUND"):
        load_provider_config(tmp_path / "missing.yaml", environ={})
    with pytest.raises(ProviderConfigError, match="PROVIDER_CONFIG_NOT_FOUND"):
        load_provider_config(
            environ={"ADVERSARIAL_PROVIDER_CONFIG": str(tmp_path / "missing.yaml")}
        )


def test_role_json_override_replaces_only_that_chain(tmp_path):
    path = _write_config(tmp_path / "providers.yaml")
    override = json.dumps([{"alias": "test", "cmd": "echo hello"}])

    loaded = load_provider_config(
        path, environ={"ADVERSARIAL_DEV_PROVIDERS": override}
    )

    assert loaded is not None
    assert [entry.alias for entry in loaded["dev"]] == ["test"]
    assert [entry.alias for entry in loaded["review"]] == [
        "review-a",
        "review-b",
    ]


def test_override_can_create_registry_without_default(monkeypatch, tmp_path):
    home = str(tmp_path / "empty-home")
    monkeypatch.setenv("HOME", home)
    loaded = load_provider_config(
        environ={
            "HOME": home,
            "ADVERSARIAL_VERIFY_PROVIDERS": (
                '[{"alias":"verify", "command":"echo verify"}]'
            )
        }
    )
    assert loaded is not None
    assert loaded["verify"][0].command == "echo verify"
    assert loaded.source_path is None


@pytest.mark.parametrize(
    ("content", "code"),
    [
        ("dev: [", "PROVIDER_CONFIG_INVALID_YAML"),
        ("- dev", "PROVIDER_CONFIG_INVALID_ROOT"),
        ("dev:\n  - cmd: echo hi\n", "PROVIDER_CONFIG_MISSING_ALIAS"),
        ("dev:\n  - alias: test\n", "PROVIDER_CONFIG_MISSING_COMMAND"),
        (
            "dev:\n  - alias: test\n    cmd: '  '\n",
            "PROVIDER_CONFIG_INVALID_COMMAND",
        ),
        (
            "dev:\n  - alias: test\n    cmd: echo hi\n    stop_threshold: -1\n",
            "PROVIDER_CONFIG_INVALID_THRESHOLD",
        ),
        (
            "dev:\n  - alias: test\n    cmd: echo hi\nquota_cache_ttl: -1\n",
            "PROVIDER_CONFIG_INVALID_TTL",
        ),
        (
            "dev:\n  - alias: test\n    cmd: echo hi\n"
            "  - alias: test\n    cmd: echo again\n",
            "PROVIDER_CONFIG_DUPLICATE_ALIAS",
        ),
    ],
)
def test_invalid_config_has_stable_error_code(tmp_path, content, code):
    path = tmp_path / "providers.yaml"
    path.write_text(content)

    with pytest.raises(ProviderConfigError) as caught:
        load_provider_config(path, environ={})

    assert caught.value.code == code
    assert str(caught.value).startswith(f"{code}:")


def test_invalid_override_has_stable_error_code(tmp_path):
    path = _write_config(tmp_path / "providers.yaml")
    with pytest.raises(ProviderConfigError) as caught:
        load_provider_config(
            path, environ={"ADVERSARIAL_DEV_PROVIDERS": "not json"}
        )
    assert caught.value.code == "PROVIDER_CONFIG_INVALID_OVERRIDE_JSON"


def test_unrelated_provider_shaped_environment_variable_is_ignored(tmp_path):
    path = _write_config(tmp_path / "providers.yaml")

    loaded = load_provider_config(
        path, environ={"ADVERSARIAL_CACHE_PROVIDERS": "not json"}
    )

    assert loaded is not None
    assert loaded["dev"][0].alias == "dev-a"


def test_configured_custom_role_override_is_validated(tmp_path):
    path = _write_config(
        tmp_path / "providers.yaml",
        custom=[{"alias": "custom", "cmd": "echo custom"}],
    )

    with pytest.raises(ProviderConfigError) as caught:
        load_provider_config(
            path, environ={"ADVERSARIAL_CUSTOM_PROVIDERS": "not json"}
        )

    assert caught.value.code == "PROVIDER_CONFIG_INVALID_OVERRIDE_JSON"


def test_tilde_path_is_expanded(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    resolved, explicit = resolve_provider_config_path("~/test/providers.yaml")
    assert resolved == (tmp_path / "test/providers.yaml").resolve()
    assert explicit is True


def test_injected_environment_without_home_does_not_use_process_home(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("HOME", str(tmp_path / "process-home"))

    with pytest.raises(ProviderConfigError) as caught:
        resolve_provider_config_path(environ={})

    assert caught.value.code == "PROVIDER_CONFIG_HOME_UNSET"


def test_role_accessors_raise_consistently_for_missing_role():
    config = ProviderConfig(
        roles={"dev": (ProviderEntry("dev", "echo dev"),)}
    )

    with pytest.raises(KeyError):
        config.chain_for("review")
    with pytest.raises(KeyError):
        config["review"]


def test_stop_threshold_has_supported_percentage_upper_bound():
    with pytest.raises(ProviderConfigError) as caught:
        ProviderEntry("dev", "echo dev", stop_threshold=101)

    assert caught.value.code == "PROVIDER_CONFIG_INVALID_THRESHOLD"


def test_resolve_role_cmd_uses_registry_between_legacy_inputs_and_default(tmp_path):
    path = _write_config(
        tmp_path / "providers.yaml",
        dev=[{"alias": "registry", "cmd": "echo registry command"}],
    )

    assert resolve_role_cmd(
        "dev",
        None,
        "DEV_CMD",
        "echo default",
        config_path=path,
        environ={},
    ) == "echo registry command"
    assert resolve_role_cmd(
        "dev",
        "echo flag",
        "DEV_CMD",
        config_path=path,
        environ={"DEV_CMD": "echo env"},
    ) == "echo flag"
    assert resolve_role_cmd(
        "dev",
        None,
        "DEV_CMD",
        config_path=path,
        environ={"DEV_CMD": "echo env"},
    ) == "echo env"


def test_resolve_role_cmd_accepts_preloaded_registry():
    config = ProviderConfig(
        roles={
            "dev": (
                ProviderEntry("first", "echo first"),
                ProviderEntry("second", "echo second"),
            )
        }
    )

    assert resolve_role_cmd(
        "dev", None, "DEV_CMD", provider_config=config, environ={}
    ) == "echo first"


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
