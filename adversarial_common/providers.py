"""Provider detection, persona injection, role resolution, and usage parsing."""

import json
import os
import re
import shlex
import sys
from pathlib import Path
from typing import Final


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


def inject_persona(argv, persona_file, stdin_text):
    """Inject a persona natively for Claude and through stdin otherwise."""
    if detect_provider(argv) == "claude":
        return argv + ["--append-system-prompt-file", persona_file], stdin_text
    try:
        persona_text = Path(persona_file).read_text()
    except OSError:
        persona_text = ""
    if persona_text:
        stdin_text = f"{persona_text}\n\n{stdin_text or ''}"
    return argv, stdin_text


def enhance_cmd_for_project(cmd, project_path):
    """Add provider-specific project access flags."""
    provider = detect_provider(cmd)
    if provider == "claude" and "--allowedTools" not in cmd:
        return f"{cmd} --allowedTools Read,Bash"
    if provider == "codex" and "-C" not in cmd:
        return f"{cmd} -C {shlex.quote(str(project_path))}"
    return cmd


def resolve_role_cmd(role, flag_value, env_var, default=None):
    """Resolve a role command using flag, environment, then default."""
    cmd = (flag_value or os.environ.get(env_var) or default or "").strip()
    if not cmd:
        print(
            f"X No command configured for role '{role}' "
            f"(pass the CLI flag or set ${env_var})"
        )
        sys.exit(1)
    return shlex.join(os.path.expanduser(token) for token in shlex.split(cmd))


def default_wrapper_cmd(extra_flags=""):
    """Return the default Claude wrapper command without pinning a model."""
    wrapper = os.path.expanduser(
        "~/.hermes/skills/autonomous-ai-agents/hermes-agent/scripts/claude-tmux.py"
    )
    cmd = f"python3 {wrapper} --yolo"
    return f"{cmd} {extra_flags}".strip()


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
    "classify_transient_error", "default_wrapper_cmd", "detect_provider",
    "enhance_cmd_for_project", "extract_usage_metadata", "inject_persona",
    "is_transient_error", "persona_for_role", "resolve_role_cmd", "run_cmd",
]
