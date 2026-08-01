"""Provider registry loading and provider runtime helpers."""

import importlib
import json
import math
import os
import re
import shlex
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Final, Mapping


DEFAULT_PROVIDER_CONFIG_PATH: Final = Path("~/.config/adversarial/providers.yaml")
PROVIDER_CONFIG_ENV: Final = "ADVERSARIAL_PROVIDER_CONFIG"
CLAUDE_TMUX_PATH_ENV: Final = "ADVERSARIAL_CLAUDE_TMUX_PATH"
_CLAUDE_TMUX_EXECUTABLE: Final = "claude-tmux.py"
_TRUSTED_PERSONA_END: Final = "--- END TRUSTED PERSONA ---"
_UNTRUSTED_BODY_BEGIN: Final = "--- BEGIN UNTRUSTED CONTENT ---"
_UNTRUSTED_BODY_END: Final = "--- END UNTRUSTED CONTENT ---"
_ROLE_OVERRIDE_PREFIX: Final = "ADVERSARIAL_"
_ROLE_OVERRIDE_SUFFIX: Final = "_PROVIDERS"
_ROLE_NAME_RE: Final = re.compile(r"^[a-z][a-z0-9_]*$")
_KNOWN_PROVIDER_ROLES: Final = frozenset(
    {
        "architect",
        "arbiter",
        "builder",
        "critic",
        "cross_review",
        "dev",
        "fixer",
        "inspector",
        "judge",
        "plan_challenger",
        "plan_writer",
        "research",
        "review",
        "spec_challenger",
        "spec_writer",
        "synthesis",
        "verifier",
        "verify",
    }
)
_CONFIG_KEYS: Final = frozenset({"quota_cmd", "quota_cache_ttl", "roles"})
_ENTRY_KEYS: Final = frozenset(
    {"alias", "cmd", "command", "quota_check", "stop_threshold"}
)


class ProviderConfigError(ValueError):
    """A provider configuration error with a stable machine-readable code."""

    def __init__(self, code: str, detail: str):
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}")


@dataclass(frozen=True, slots=True)
class ProviderEntry:
    """One immutable provider candidate in a role's preference chain."""

    alias: str
    command: str
    quota_check: str | None = None
    stop_threshold: int | float | None = None

    def __post_init__(self) -> None:
        alias = _required_text(self.alias, "PROVIDER_CONFIG_INVALID_ALIAS", "alias")
        command = _required_text(
            self.command, "PROVIDER_CONFIG_INVALID_COMMAND", "command"
        )
        quota_check = self.quota_check
        if quota_check is not None:
            quota_check = _required_text(
                quota_check,
                "PROVIDER_CONFIG_INVALID_QUOTA_CHECK",
                "quota_check",
            )
        threshold = self.stop_threshold
        if threshold is not None:
            threshold = _number(
                threshold,
                "PROVIDER_CONFIG_INVALID_THRESHOLD",
                "stop_threshold",
            )
            if not 0 <= threshold <= 100:
                raise ProviderConfigError(
                    "PROVIDER_CONFIG_INVALID_THRESHOLD",
                    "stop_threshold must be between 0 and 100",
                )
        object.__setattr__(self, "alias", alias)
        object.__setattr__(self, "command", command)
        object.__setattr__(self, "quota_check", quota_check)
        object.__setattr__(self, "stop_threshold", threshold)

    @property
    def cmd(self) -> str:
        """Return the YAML-compatible name for :attr:`command`."""
        return self.command

    def to_dict(self) -> dict[str, object]:
        """Return a YAML/JSON-safe representation of this entry."""
        result: dict[str, object] = {"alias": self.alias, "cmd": self.command}
        if self.quota_check is not None:
            result["quota_check"] = self.quota_check
        if self.stop_threshold is not None:
            result["stop_threshold"] = self.stop_threshold
        return result


@dataclass(frozen=True, slots=True)
class ProviderConfig:
    """An immutable registry of ordered provider chains keyed by role."""

    roles: Mapping[str, tuple[ProviderEntry, ...]] = field(default_factory=dict)
    quota_cmd: str | None = None
    quota_cache_ttl: int | float = 30
    source_path: Path | None = field(default=None, compare=False)

    def __post_init__(self) -> None:
        if not isinstance(self.roles, Mapping):
            raise ProviderConfigError(
                "PROVIDER_CONFIG_INVALID_ROLES", "roles must be a mapping"
            )
        immutable_roles: dict[str, tuple[ProviderEntry, ...]] = {}
        for role, entries in self.roles.items():
            normalized_role = _role_name(role)
            if isinstance(entries, (str, bytes)):
                raise ProviderConfigError(
                    "PROVIDER_CONFIG_INVALID_CHAIN",
                    f"role '{normalized_role}' must contain an array",
                )
            try:
                chain = tuple(entries)
            except TypeError as exc:
                raise ProviderConfigError(
                    "PROVIDER_CONFIG_INVALID_CHAIN",
                    f"role '{normalized_role}' must contain an array",
                ) from exc
            if not chain:
                raise ProviderConfigError(
                    "PROVIDER_CONFIG_EMPTY_CHAIN",
                    f"role '{normalized_role}' must contain at least one provider",
                )
            if not all(isinstance(entry, ProviderEntry) for entry in chain):
                raise ProviderConfigError(
                    "PROVIDER_CONFIG_INVALID_ENTRY",
                    f"role '{normalized_role}' contains a non-ProviderEntry value",
                )
            aliases: set[str] = set()
            for entry in chain:
                if entry.alias in aliases:
                    raise ProviderConfigError(
                        "PROVIDER_CONFIG_DUPLICATE_ALIAS",
                        f"role '{normalized_role}' contains duplicate alias "
                        f"'{entry.alias}'",
                    )
                aliases.add(entry.alias)
            immutable_roles[normalized_role] = chain

        quota_cmd = self.quota_cmd
        if quota_cmd is not None:
            quota_cmd = _required_text(
                quota_cmd, "PROVIDER_CONFIG_INVALID_QUOTA_CMD", "quota_cmd"
            )
        ttl = _number(
            self.quota_cache_ttl,
            "PROVIDER_CONFIG_INVALID_TTL",
            "quota_cache_ttl",
        )
        if ttl < 0:
            raise ProviderConfigError(
                "PROVIDER_CONFIG_INVALID_TTL", "quota_cache_ttl must be >= 0"
            )
        source_path = self.source_path
        if source_path is not None:
            source_path = Path(source_path).expanduser().resolve(strict=False)

        object.__setattr__(self, "roles", MappingProxyType(immutable_roles))
        object.__setattr__(self, "quota_cmd", quota_cmd)
        object.__setattr__(self, "quota_cache_ttl", ttl)
        object.__setattr__(self, "source_path", source_path)

    def chain_for(self, role: str) -> tuple[ProviderEntry, ...]:
        """Return a role's ordered chain, raising ``KeyError`` if absent."""
        return self.roles[_role_name(role)]

    def __getitem__(self, role: str) -> tuple[ProviderEntry, ...]:
        return self.roles[_role_name(role)]

    def to_dict(self) -> dict[str, object]:
        """Return the external YAML representation of the registry."""
        result: dict[str, object] = {
            role: [entry.to_dict() for entry in entries]
            for role, entries in self.roles.items()
        }
        if self.quota_cmd is not None:
            result["quota_cmd"] = self.quota_cmd
        result["quota_cache_ttl"] = self.quota_cache_ttl
        return result

    @classmethod
    def load(
        cls,
        path: str | os.PathLike[str] | None = None,
        *,
        environ: Mapping[str, str] | None = None,
    ) -> "ProviderConfig | None":
        """Load a registry using CLI-path, environment, then default precedence."""
        return load_provider_config(path, environ=environ)


# Registry is a useful semantic name for callers that do not own configuration.
ProviderRegistry = ProviderConfig


def resolve_provider_config_path(
    path: str | os.PathLike[str] | None = None,
    *,
    environ: Mapping[str, str] | None = None,
) -> tuple[Path, bool]:
    """Return ``(resolved_path, explicitly_selected)`` for the registry."""
    environment = os.environ if environ is None else environ
    if path is not None:
        try:
            raw_path = os.fspath(path)
        except TypeError as exc:
            raise ProviderConfigError(
                "PROVIDER_CONFIG_INVALID_PATH",
                "provider config path must be a path string",
            ) from exc
        explicit = True
    elif PROVIDER_CONFIG_ENV in environment:
        raw_path = environment[PROVIDER_CONFIG_ENV]
        explicit = True
    else:
        raw_path = os.fspath(DEFAULT_PROVIDER_CONFIG_PATH)
        explicit = False
    if not isinstance(raw_path, str) or not raw_path.strip():
        raise ProviderConfigError(
            "PROVIDER_CONFIG_INVALID_PATH", "provider config path must be non-empty"
        )
    return _expand_config_path(raw_path, environment), explicit


def load_provider_config(
    path: str | os.PathLike[str] | None = None,
    *,
    environ: Mapping[str, str] | None = None,
) -> ProviderConfig | None:
    """Load, validate, and apply role overrides to one selected YAML file.

    ``path`` represents the CLI selection and therefore has highest precedence.
    A missing implicit default enables legacy mode and returns ``None``.  A path
    selected by the argument or environment is explicit and must exist.
    """
    environment = os.environ if environ is None else environ
    selected_path, explicit = resolve_provider_config_path(path, environ=environment)
    if not selected_path.is_file():
        if explicit:
            raise ProviderConfigError(
                "PROVIDER_CONFIG_NOT_FOUND",
                f"provider config file not found: {selected_path}",
            )
        raw_config: dict[str, object] = {}
        source_path: Path | None = None
    else:
        source_path = selected_path
        try:
            yaml = importlib.import_module("yaml")
        except ImportError as exc:
            raise ProviderConfigError(
                "PROVIDER_CONFIG_YAML_UNAVAILABLE",
                "YAML support is unavailable; install PyYAML",
            ) from exc
        try:
            with selected_path.open("r", encoding="utf-8") as stream:
                loaded = yaml.safe_load(stream)
        except yaml.YAMLError as exc:
            raise ProviderConfigError(
                "PROVIDER_CONFIG_INVALID_YAML", "provider config is malformed YAML"
            ) from exc
        except (OSError, UnicodeError) as exc:
            raise ProviderConfigError(
                "PROVIDER_CONFIG_READ_ERROR",
                f"provider config could not be read: {selected_path}",
            ) from exc
        if loaded is None:
            raw_config = {}
        elif not isinstance(loaded, dict):
            raise ProviderConfigError(
                "PROVIDER_CONFIG_INVALID_ROOT",
                "provider config root must be a mapping",
            )
        else:
            raw_config = loaded

    roles, quota_cmd, ttl = _parse_config_mapping(raw_config)
    overrides = _load_role_overrides(environment, known_roles=roles)
    roles.update(overrides)
    if source_path is None and not roles and quota_cmd is None and ttl == 30:
        return None
    return ProviderConfig(
        roles=roles,
        quota_cmd=quota_cmd,
        quota_cache_ttl=ttl,
        source_path=source_path,
    )


def _parse_config_mapping(
    value: Mapping[object, object],
) -> tuple[dict[str, tuple[ProviderEntry, ...]], str | None, int | float]:
    nested_roles = value.get("roles")
    if "roles" in value and not isinstance(nested_roles, dict):
        raise ProviderConfigError(
            "PROVIDER_CONFIG_INVALID_ROLES", "roles must be a mapping"
        )
    role_values: dict[object, object] = dict(nested_roles or {})
    for key, chain in value.items():
        if key in _CONFIG_KEYS:
            continue
        if not isinstance(key, str):
            raise ProviderConfigError(
                "PROVIDER_CONFIG_INVALID_ROLE", "role names must be strings"
            )
        if key in role_values:
            raise ProviderConfigError(
                "PROVIDER_CONFIG_DUPLICATE_ROLE",
                f"role '{key}' is configured more than once",
            )
        role_values[key] = chain
    roles = {
        _role_name(role): _parse_chain(chain, str(role))
        for role, chain in role_values.items()
    }
    raw_quota_cmd = value.get("quota_cmd")
    quota_cmd = None
    if raw_quota_cmd is not None:
        quota_cmd = _required_text(
            raw_quota_cmd, "PROVIDER_CONFIG_INVALID_QUOTA_CMD", "quota_cmd"
        )
    ttl = _number(
        value.get("quota_cache_ttl", 30),
        "PROVIDER_CONFIG_INVALID_TTL",
        "quota_cache_ttl",
    )
    if ttl < 0:
        raise ProviderConfigError(
            "PROVIDER_CONFIG_INVALID_TTL", "quota_cache_ttl must be >= 0"
        )
    return roles, quota_cmd, ttl


def _load_role_overrides(
    environment: Mapping[str, str],
    *,
    known_roles: Mapping[str, object],
) -> dict[str, tuple[ProviderEntry, ...]]:
    overrides: dict[str, tuple[ProviderEntry, ...]] = {}
    for variable, encoded in environment.items():
        if not (
            variable.startswith(_ROLE_OVERRIDE_PREFIX)
            and variable.endswith(_ROLE_OVERRIDE_SUFFIX)
        ):
            continue
        role_part = variable[
            len(_ROLE_OVERRIDE_PREFIX) : -len(_ROLE_OVERRIDE_SUFFIX)
        ]
        if not role_part:
            continue
        candidate = role_part.lower()
        if not _ROLE_NAME_RE.fullmatch(candidate):
            continue
        role = candidate
        if role not in known_roles and role not in _KNOWN_PROVIDER_ROLES:
            continue
        try:
            value = json.loads(encoded)
        except (TypeError, json.JSONDecodeError) as exc:
            raise ProviderConfigError(
                "PROVIDER_CONFIG_INVALID_OVERRIDE_JSON",
                f"{variable} must contain a JSON array",
            ) from exc
        overrides[role] = _parse_chain(value, role, source=variable)
    return overrides


def _parse_chain(
    value: object, role: str, *, source: str | None = None
) -> tuple[ProviderEntry, ...]:
    location = source or f"role '{role}'"
    if not isinstance(value, list):
        raise ProviderConfigError(
            "PROVIDER_CONFIG_INVALID_CHAIN", f"{location} must contain an array"
        )
    if not value:
        raise ProviderConfigError(
            "PROVIDER_CONFIG_EMPTY_CHAIN",
            f"{location} must contain at least one provider",
        )
    entries: list[ProviderEntry] = []
    aliases: set[str] = set()
    for index, item in enumerate(value):
        if not isinstance(item, dict):
            raise ProviderConfigError(
                "PROVIDER_CONFIG_INVALID_ENTRY",
                f"{location} entry {index} must be a mapping",
            )
        unknown = set(item).difference(_ENTRY_KEYS)
        if unknown:
            key = sorted(str(name) for name in unknown)[0]
            raise ProviderConfigError(
                "PROVIDER_CONFIG_UNKNOWN_ENTRY_KEY",
                f"{location} entry {index} has unknown key '{key}'",
            )
        if "alias" not in item:
            raise ProviderConfigError(
                "PROVIDER_CONFIG_MISSING_ALIAS",
                f"{location} entry {index} is missing alias",
            )
        has_cmd = "cmd" in item
        has_command = "command" in item
        if has_cmd and has_command:
            raise ProviderConfigError(
                "PROVIDER_CONFIG_DUPLICATE_COMMAND",
                f"{location} entry {index} must use only one of cmd or command",
            )
        if not has_cmd and not has_command:
            raise ProviderConfigError(
                "PROVIDER_CONFIG_MISSING_COMMAND",
                f"{location} entry {index} is missing cmd",
            )
        entry = ProviderEntry(
            alias=item["alias"],
            command=item["cmd"] if has_cmd else item["command"],
            quota_check=item.get("quota_check"),
            stop_threshold=item.get("stop_threshold"),
        )
        if entry.alias in aliases:
            raise ProviderConfigError(
                "PROVIDER_CONFIG_DUPLICATE_ALIAS",
                f"{location} contains duplicate alias '{entry.alias}'",
            )
        aliases.add(entry.alias)
        entries.append(entry)
    return tuple(entries)


def _role_name(value: object) -> str:
    if not isinstance(value, str) or not _ROLE_NAME_RE.fullmatch(value):
        raise ProviderConfigError(
            "PROVIDER_CONFIG_INVALID_ROLE",
            "role names must match ^[a-z][a-z0-9_]*$",
        )
    return value


def _expand_config_path(raw_path: str, environment: Mapping[str, str]) -> Path:
    """Expand the selected path while honoring an injected HOME in tests/CI."""
    if raw_path == "~" or raw_path.startswith("~/"):
        home = environment.get("HOME")
        if not home:
            raise ProviderConfigError(
                "PROVIDER_CONFIG_HOME_UNSET",
                "HOME must be set to expand the provider config path",
            )
        raw_path = os.path.join(home, raw_path[2:]) if raw_path != "~" else home
    elif raw_path.startswith("~"):
        raise ProviderConfigError(
            "PROVIDER_CONFIG_INVALID_PATH",
            "provider config path supports only '~' or '~/' expansion",
        )
    return Path(raw_path).resolve(strict=False)


def _required_text(value: object, code: str, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ProviderConfigError(code, f"{field_name} must be a non-empty string")
    return value.strip()


def _number(value: object, code: str, field_name: str) -> int | float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
    ):
        raise ProviderConfigError(code, f"{field_name} must be a finite number")
    return value


_NETWORK_TRANSIENT_PATTERNS: Final[tuple[re.Pattern[str], ...]] = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"connection (?:was )?(?:reset|closed|aborted|refused)",
        r"connectionreseterror",
        r"econn(?:reset|refused|aborted)",
        r"network (?:is )?(?:unreachable|error|failure)",
        r"temporary failure in name resolution",
        r"name or service not known",
        r"tls (?:error|handshake|alert)",
        r"ssl(?:error| handshake)",
        r"unexpected eof",
        r"eof (?:occurred|error|while)",
        r"broken pipe",
        r"timed? out while (?:connecting|reading|waiting)",
    )
)
_PERMANENT_ERROR_PATTERNS: Final[tuple[re.Pattern[str], ...]] = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"invalid (?:api[ -]?key|authentication|credentials)",
        r"authentication (?:failed|required)",
        r"\bunauthorized\b",
        r"\bforbidden\b",
        r"permission denied",
        r"malformed (?:request|input|json)",
    )
)
_HTTP_STATUS_RE: Final = re.compile(
    r"(?:\bhttp(?:/\d(?:\.\d)?)?\s*|\bstatus(?:\s+code)?\s*[:=]?\s*)(4\d{2})\b",
    re.IGNORECASE,
)
_RETRYABLE_HTTP_STATUSES: Final = frozenset({408, 409, 425, 429})
_PROVIDER_TRANSIENT_PATTERNS: Final[dict[str, tuple[re.Pattern[str], ...]]] = {
    "claude": tuple(
        re.compile(pattern, re.IGNORECASE)
        for pattern in (
            r"\boverloaded_error\b",
            r"\bservice overloaded\b",
            r"\brate[_ -]?limit(?:ed| exceeded)?\b",
            r"\bapi_error\b",
        )
    ),
    "claude-tmux": tuple(
        re.compile(pattern, re.IGNORECASE)
        for pattern in (
            r"\boverloaded_error\b",
            r"\bservice overloaded\b",
            r"\brate[_ -]?limit(?:ed| exceeded)?\b",
            r"\bapi_error\b",
        )
    ),
    "codex": tuple(
        re.compile(pattern, re.IGNORECASE)
        for pattern in (
            r"stream disconnected",
            r"error sending request",
            r"failed to decode response body",
            r"\brate[_ -]?limit(?:ed| exceeded)?\b",
            r"\bserver (?:is )?overloaded\b",
        )
    ),
    "pi": tuple(
        re.compile(pattern, re.IGNORECASE)
        for pattern in (
            r"fetch failed",
            r"socket hang up",
            r"\brate[_ -]?limit(?:ed| exceeded)?\b",
            r"\bservice unavailable\b",
        )
    ),
}


def detect_provider(cmd):
    """Return the provider name based only on the command executable."""
    if isinstance(cmd, str):
        try:
            tokens = shlex.split(cmd)
        except ValueError:
            return "other"
    else:
        try:
            tokens = list(cmd)
        except TypeError:
            return "other"
    if not tokens or not isinstance(tokens[0], str):
        return "other"
    executable = os.path.basename(tokens[0])
    if executable in {"claude-tmux", "claude_tmux", "claude-tmux.py", "claude_tmux.py"}:
        return "claude-tmux"
    if executable in {"codex", "claude", "pi"}:
        return executable
    return "other"


def classify_transient_error(
    cmd,
    returncode,
    stderr="",
    *,
    elapsed=None,
    timeout=None,
):
    """Return a stable retry reason for a transient provider failure.

    A status of 124 is retryable only when the provider returned substantially
    before our own timeout. This distinguishes provider-side "fast hangs"
    from a process that consumed the full execution allowance. HTTP 408, 409,
    425, and 429 retain their standard transient meaning; other 4xx responses
    are treated as permanent client failures.
    """
    text = stderr if isinstance(stderr, str) else str(stderr or "")
    if returncode == 0:
        return None
    if returncode in {126, 127}:
        return None
    if any(pattern.search(text) for pattern in _PERMANENT_ERROR_PATTERNS):
        return None

    statuses = {int(match) for match in _HTTP_STATUS_RE.findall(text)}
    if statuses:
        hard_statuses = statuses.difference(_RETRYABLE_HTTP_STATUSES)
        if hard_statuses:
            return None
        return f"http_{min(statuses)}"

    if returncode == 124:
        if _is_fast_timeout(elapsed, timeout):
            return "fast_124"
        return None
    if any(pattern.search(text) for pattern in _NETWORK_TRANSIENT_PATTERNS):
        return "network"

    provider = detect_provider(cmd)
    for pattern in _PROVIDER_TRANSIENT_PATTERNS.get(provider, ()):
        if pattern.search(text):
            return f"{provider}_transient"
    if returncode == 75:
        return "temporary_failure"
    if returncode in _RETRYABLE_HTTP_STATUSES:
        return f"exit_{returncode}"
    return None


def is_transient_error(
    cmd,
    returncode,
    stderr="",
    *,
    elapsed=None,
    timeout=None,
):
    """Return whether a failed provider invocation is safe to retry."""
    return classify_transient_error(
        cmd, returncode, stderr, elapsed=elapsed, timeout=timeout
    ) is not None


def _is_fast_timeout(elapsed, timeout):
    if isinstance(elapsed, bool) or not isinstance(elapsed, (int, float)):
        return False
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
        return False
    if elapsed < 0 or timeout <= 0:
        return False
    # Leave a generous 10% margin for process teardown and clock granularity.
    return elapsed < timeout * 0.9


def persona_for_role(role_name, cmd):
    """Return the persona filename for a role and provider."""
    return f"{role_name}-pi" if detect_provider(cmd) == "pi" else role_name


def inject_persona(argv, persona_file, stdin_text, delimiter=False):
    """Inject a persona, optionally fencing the untrusted stdin body."""
    if detect_provider(argv) == "claude":
        if delimiter:
            stdin_text = _delimit_untrusted_body(stdin_text)
        return argv + ["--append-system-prompt-file", persona_file], stdin_text
    try:
        persona_text = Path(persona_file).read_text()
    except OSError:
        persona_text = ""
    if persona_text:
        if delimiter:
            stdin_text = (
                f"{persona_text}\n\n{_TRUSTED_PERSONA_END}\n"
                f"{_delimit_untrusted_body(stdin_text)}"
            )
        else:
            stdin_text = f"{persona_text}\n\n{stdin_text or ''}"
    elif delimiter:
        stdin_text = _delimit_untrusted_body(stdin_text)
    return argv, stdin_text


# ponytail: a zero-width break inserted into the middle of each sentinel literal
# so a forged copy embedded in untrusted body no longer matches the real marker
# exactly when the stream is re-split on the sentinels. Invisible to readers,
# breaks the substring. Ceiling: a model/consumer that strips U+200B before
# matching could re-forge; upgrade to a signed/hashed fence if that surfaces.
_FORGED_MARKER_BREAK: Final = "\u200b"


def _neutralize_forged_markers(body):
    """Break any literal sentinel copied into untrusted body so it can't forge a
    trusted boundary. The real markers added by _delimit_untrusted_body are never
    passed through here, so they stay exact."""
    for marker in (_TRUSTED_PERSONA_END, _UNTRUSTED_BODY_BEGIN, _UNTRUSTED_BODY_END):
        if marker in body:
            mid = len(marker) // 2
            body = body.replace(
                marker, marker[:mid] + _FORGED_MARKER_BREAK + marker[mid:]
            )
    return body


def _delimit_untrusted_body(stdin_text):
    body = _neutralize_forged_markers(stdin_text or "")
    return f"{_UNTRUSTED_BODY_BEGIN}\n{body}\n{_UNTRUSTED_BODY_END}"


def enhance_cmd_for_project(cmd, project_path):
    """Add provider-specific project access flags."""
    provider = detect_provider(cmd)
    if provider == "claude" and "--allowedTools" not in cmd:
        return f"{cmd} --allowedTools Read,Bash"
    if provider == "codex" and "-C" not in cmd:
        return f"{cmd} -C {shlex.quote(str(project_path))}"
    return cmd


def resolve_role_cmd(
    role,
    flag_value,
    env_var,
    default=None,
    *,
    provider_config=None,
    config_path=None,
    environ=None,
):
    """Resolve a role command using flag, environment, registry, then default.

    When neither the CLI flag nor its legacy environment variable is set, the
    configured role chain's first (most-preferred) provider is selected.  A
    supplied ``provider_config`` avoids reloading the registry when callers
    resolve several roles together.
    """
    environment = os.environ if environ is None else environ
    cmd = flag_value or environment.get(env_var)
    if not cmd:
        registry = provider_config
        if registry is None:
            registry = load_provider_config(config_path, environ=environment)
        if registry is not None:
            chain = registry.roles.get(role, ())
            if chain:
                cmd = chain[0].command
    cmd = (cmd or default or "").strip()
    if not cmd:
        print(
            f"X No command configured for role '{role}' "
            f"(pass the CLI flag or set ${env_var})"
        )
        sys.exit(1)
    return shlex.join(os.path.expanduser(token) for token in shlex.split(cmd))


def default_wrapper_cmd(extra_flags="", *, environ=None):
    """Return the discovered Claude wrapper command without pinning a model.

    ``environ`` (default ``os.environ``) drives the
    ``$ADVERSARIAL_CLAUDE_TMUX_PATH`` override and the HOME-derived fallback
    path so callers need not monkeypatch module state. Raises ``RuntimeError``
    distinguishing a PATH miss from a missing fallback when nothing resolves.
    """
    environment = os.environ if environ is None else environ
    override = environment.get(CLAUDE_TMUX_PATH_ENV, "").strip()
    if override:
        # ponytail: override tilde expansion still reads the process HOME; the
        # override path is virtually always absolute, so environ-HOME symmetry
        # for it is YAGNI (only the fallback path needs injectable HOME).
        wrapper = os.path.expanduser(override)
    else:
        wrapper = shutil.which(_CLAUDE_TMUX_EXECUTABLE)
        home = environment.get("HOME")
        fallback = (
            Path(home) if home else Path.home()
        ) / "claude-tmux-wrapper" / _CLAUDE_TMUX_EXECUTABLE
        if wrapper is None and fallback.is_file():
            wrapper = str(fallback)
        if wrapper is None:
            raise RuntimeError(
                f"{_CLAUDE_TMUX_EXECUTABLE} not found on PATH and fallback file "
                f"does not exist at {fallback}; set ${CLAUDE_TMUX_PATH_ENV} or "
                f"install the wrapper (the pre-migration hardcoded path is no "
                f"longer checked)"
            )

    flags = [flag for flag in shlex.split(extra_flags) if flag != "--yolo"]
    return shlex.join([wrapper, *flags])


def extract_usage_metadata(stdout="", stderr="", *, provider=None):
    """Extract normalized native token usage from Claude or Codex output.

    Both CLIs have emitted several JSON and JSONL shapes over time. The
    provider-specific adapters deliberately accept only complete,
    non-negative input/output pairs. A generic adapter is retained when no
    provider is supplied for callers that already use this public helper.
    """
    if provider is not None and not isinstance(provider, str):
        provider = detect_provider(provider)
    provider_name = (provider or "").strip().lower()
    if provider_name == "claude-tmux":
        provider_name = "claude"
    adapters = {
        "claude": _extract_claude_usage,
        "codex": _extract_codex_usage,
    }
    adapter = adapters.get(provider_name, _extract_generic_usage)
    for payload in _json_payloads(stderr, stdout):
        usage = adapter(payload)
        if usage is not None:
            return usage

    # Some wrapper versions print a compact usage object as diagnostic text.
    # Restrict this fallback to known token field names and require both sides
    # so an unrelated log counter is never mistaken for billable usage.
    combined = "\n".join((stdout or "", stderr or ""))
    return _usage_from_text(combined)


def _json_payloads(*streams):
    for text in streams:
        if not isinstance(text, str) or not text.strip():
            continue
        try:
            yield json.loads(text)
        except json.JSONDecodeError:
            pass
        for line in reversed(text.splitlines()):
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


def _extract_claude_usage(value):
    if not isinstance(value, (dict, list)):
        return None
    if isinstance(value, dict):
        usage = _normalized_usage(value.get("usage"))
        if usage is not None:
            return usage
        # Claude's result envelope can expose per-model usage using camelCase.
        model_usage = value.get("modelUsage")
        if isinstance(model_usage, dict):
            prompt = completion = 0
            found = False
            for item in model_usage.values():
                normalized = _normalized_usage(item)
                if normalized is not None:
                    prompt += normalized["prompt_tokens"]
                    completion += normalized["completion_tokens"]
                    found = True
            if found:
                return {
                    "prompt_tokens": prompt,
                    "completion_tokens": completion,
                }
    return _find_usage(value)


def _extract_codex_usage(value):
    if isinstance(value, dict):
        for key in ("usage", "token_usage", "tokenUsage"):
            usage = _normalized_usage(value.get(key))
            if usage is not None:
                return usage
    return _find_usage(value)


def _extract_generic_usage(value):
    return _find_usage(value)


def _find_usage(value):
    if isinstance(value, dict):
        usage = _normalized_usage(value)
        if usage is not None:
            return usage
        for child in value.values():
            usage = _find_usage(child)
            if usage is not None:
                return usage
    elif isinstance(value, list):
        # Later JSONL/list events generally hold the cumulative turn total.
        for child in reversed(value):
            usage = _find_usage(child)
            if usage is not None:
                return usage
    return None


def _normalized_usage(value):
    if not isinstance(value, dict):
        return None
    prompt = _first_token_count(
        value, "prompt_tokens", "input_tokens", "inputTokens"
    )
    completion = _first_token_count(
        value, "completion_tokens", "output_tokens", "outputTokens"
    )
    if prompt is None or completion is None:
        return None
    return {"prompt_tokens": prompt, "completion_tokens": completion}


def _first_token_count(value, *keys):
    for key in keys:
        count = value.get(key)
        if isinstance(count, int) and not isinstance(count, bool) and count >= 0:
            return count
    return None


def _usage_from_text(text):
    found = {}
    for canonical, names in {
        "prompt_tokens": ("prompt_tokens", "input_tokens", "inputTokens"),
        "completion_tokens": (
            "completion_tokens", "output_tokens", "outputTokens"
        ),
    }.items():
        for name in names:
            match = re.search(rf'"?{name}"?\s*[:=]\s*(\d+)', text)
            if match:
                found[canonical] = int(match.group(1))
                break
    return found if len(found) == 2 else None


def run_cmd(
    cmd,
    stdin_text=None,
    timeout=600,
    cwd=None,
    role=None,
    project=None,
    **execution,
):
    """Run a configured role through the canonical hardened runner."""
    from . import runner

    if project:
        cmd = enhance_cmd_for_project(cmd, project)
    persona_file = None
    if role:
        try:
            from . import persona_path
            persona_file = persona_path(persona_for_role(role, cmd))
        except FileNotFoundError:
            persona_file = None
    return runner.run_cli(
        cmd,
        stdin_text=stdin_text,
        timeout=timeout,
        cwd=cwd,
        persona_file=persona_file,
        persona=execution.pop("persona", role or ""),
        **execution,
    )


__all__ = [
    "CLAUDE_TMUX_PATH_ENV", "DEFAULT_PROVIDER_CONFIG_PATH",
    "PROVIDER_CONFIG_ENV", "ProviderConfig",
    "ProviderConfigError", "ProviderEntry", "ProviderRegistry",
    "classify_transient_error", "default_wrapper_cmd", "detect_provider",
    "enhance_cmd_for_project", "extract_usage_metadata", "inject_persona",
    "is_transient_error", "load_provider_config", "persona_for_role",
    "resolve_provider_config_path", "resolve_role_cmd", "run_cmd",
]
