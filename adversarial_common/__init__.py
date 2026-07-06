"""Shared engine for the adversarial-code-loop and adversarial-code-review skills.

Modules:
  runner    — hardened subprocess execution (Popen + temp-file IO + killpg)
  jsonio    — JSON extraction, artifact save/resume, final.json emission
  providers — provider detection, persona injection, project-access flags
  snapshot  — git working-tree baseline snapshot

Personas live as plain-text files in ../personas/ and are the single source
of truth — SKILL.md links to them, scripts load them at runtime.
"""

from pathlib import Path

PERSONAS_DIR = Path(__file__).resolve().parent.parent / "personas"


def persona_path(name):
    """Absolute path to a persona file (e.g. 'builder' -> personas/builder.md)."""
    path = PERSONAS_DIR / f"{name}.md"
    if not path.is_file():
        raise FileNotFoundError(f"Persona not found: {path}")
    return str(path)


def load_persona(name):
    """Return the persona text for `name`."""
    return Path(persona_path(name)).read_text()
