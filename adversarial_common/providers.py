"""Provider detection, persona injection, and role command resolution.

Unknown providers always get the safe universal path (persona prefixed to
stdin, no extra flags), so new CLIs work without code changes.
"""

import os
import shlex
import sys
from pathlib import Path


def detect_provider(cmd):
    """Return 'claude-tmux', 'codex', 'claude', 'pi', or 'other' from a command.

    `cmd` may be a string or an argv list. claude-tmux is checked first (its
    path contains 'claude'), then codex before claude so a codex binary living
    under a claude-named path is not misclassified. 'pi' is checked before
    'other' since pi is a known coding agent that uses tool-based file writes.
    """
    if not isinstance(cmd, str):
        cmd = " ".join(cmd)
    if 'claude-tmux' in cmd or 'claude_tmux' in cmd:
        return 'claude-tmux'
    if 'codex' in cmd:
        return 'codex'
    if 'claude' in cmd:
        return 'claude'
    if 'pi ' in cmd or cmd.strip() == 'pi' or '/pi ' in cmd:
        return 'pi'
    return 'other'


def persona_for_role(role_name, cmd):
    """Return the persona filename for a role, considering the provider.

    pi/GLM gets specialised personas that instruct tool-based file writes
    instead of producing code in markdown/JSON output. All other providers
    use the standard personas.
    """
    provider = detect_provider(cmd)
    if provider == 'pi':
        return f"{role_name}-pi"
    return role_name


def inject_persona(argv, persona_file, stdin_text):
    """Inject a persona into a command. Returns (argv, stdin_text).

    Only the native `claude` CLI supports --append-system-prompt-file.
    claude-tmux.py (strict argparse), codex, and unknown providers get the
    persona text prefixed to stdin instead.
    """
    provider = detect_provider(argv)
    if provider == "claude":
        return argv + ["--append-system-prompt-file", persona_file], stdin_text
    try:
        persona_text = Path(persona_file).read_text()
    except OSError:
        persona_text = ""
    if persona_text:
        stdin_text = f"{persona_text}\n\n{stdin_text or ''}"
    return argv, stdin_text


def enhance_cmd_for_project(cmd, project_path):
    """Add tool-enabling flags so the model can explore project files itself.

    claude-tmux: no-op (interactive Claude already has all tools)
    claude (native -p): --allowedTools Read,Bash (no --max-turns: it truncates
        piped review output)
    codex: -C <project_root> for context directory
    other: no-op (safe universal path)
    """
    provider = detect_provider(cmd)
    if provider == 'claude' and '--allowedTools' not in cmd:
        return f"{cmd} --allowedTools Read,Bash"
    if provider == 'codex' and '-C' not in cmd:
        return f"{cmd} -C {shlex.quote(str(project_path))}"
    return cmd


def resolve_role_cmd(role, flag_value, env_var, default=None):
    """Resolve a role command: CLI flag > env var > default > error.

    Built-in defaults must never name a model — each CLI picks its own best.
    Exits with a clear message when nothing resolves to a non-empty command.
    """
    cmd = flag_value or os.environ.get(env_var) or default or ""
    cmd = cmd.strip()
    if not cmd:
        print(f"X No command configured for role '{role}' "
              f"(pass the CLI flag or set ${env_var})")
        sys.exit(1)
    return os.path.expanduser(cmd) if cmd.startswith("~") else cmd


def default_wrapper_cmd(extra_flags=""):
    """Default Claude command via the claude-tmux wrapper, resolved from ~.

    No --model flag: the CLI's own default is the right model.
    """
    wrapper = os.path.expanduser(
        "~/.hermes/skills/autonomous-ai-agents/hermes-agent/scripts/claude-tmux.py")
    cmd = f"python3 {wrapper} --yolo"
    return f"{cmd} {extra_flags}".strip()


def run_cmd(cmd, stdin_text=None, timeout=600, cwd=None, role=None, project=None):
    """Run a role command end-to-end and return ``(stdout, stderr, returncode)``.

    High-level convenience for phase modules: applies project-access flags
    (``enhance_cmd_for_project``), resolves the provider-appropriate persona for
    ``role`` ('builder'/'fixer'/'critic'/'verifier'/'judge'), injects it, and
    delegates to the hardened :func:`runner.run_cli`.

    ``runner`` is imported lazily to avoid a circular import (it imports this
    module). A missing persona file degrades gracefully to no persona rather
    than raising.
    """
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
        cmd, stdin_text=stdin_text, timeout=timeout, cwd=cwd,
        persona_file=persona_file,
    )
