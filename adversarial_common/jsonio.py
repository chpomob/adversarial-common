"""JSON extraction and artifact handling shared by both adversarial pipelines.

Includes YAML frontmatter parsing (requires PyYAML), structured text extraction,
and artifact management for the adversarial-review/loop/plan/spec pipelines.
"""
import json
import re
from pathlib import Path

import yaml  # hard dependency — no try/except fallback


def strip_json_wrapper(text):
    """Strip markdown code fences / prose preamble from LLM JSON output.

    Models often wrap JSON in ```json fences or precede it with prose.
    Returns the largest valid JSON object found in the text (canonicalized),
    or the original text when no JSON object can be decoded.
    """
    if not text:
        return text
    cleaned = re.sub(r'```(?:json)?\s*\n?', '', text)
    cleaned = re.sub(r'\n?```', '', cleaned)
    candidates = []
    pos = 0
    decoder = json.JSONDecoder()
    while pos < len(cleaned):
        brace = cleaned.find('{', pos)
        if brace == -1:
            break
        try:
            obj, end = decoder.raw_decode(cleaned[brace:])
            candidates.append(json.dumps(obj))
            pos = brace + end
        except json.JSONDecodeError:
            pos = brace + 1
    if not candidates:
        return text
    return max(candidates, key=len)


def save_artifact(out_dir, name, content):
    """Write an artifact file under out_dir, creating parents as needed."""
    path = Path(out_dir) / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    return path


def resume_artifact(out_dir, filename, label):
    """Return saved artifact text if it exists and is non-empty (for --resume)."""
    path = Path(out_dir) / filename
    if path.is_file():
        text = path.read_text()
        if text.strip():
            print(f"  >> Resuming '{label}' from existing {filename}")
            return text
    return None


def write_final_json(out_dir, verdict, **extra):
    """Write the machine-readable final.json verdict artifact.

    Orchestrators (CI, cron) should consume this instead of parsing exit codes
    or stdout. `extra` fields are merged into the JSON object.
    """
    payload = {"verdict": verdict}
    payload.update(extra)
    return save_artifact(out_dir, "final.json", json.dumps(payload, indent=2) + "\n")


# ---------------------------------------------------------------------------
# 3-strategy JSON extraction (Item 5)
# ---------------------------------------------------------------------------

def parse_json_output(text: str) -> dict | list | None:
    """Parse JSON from model output, trying multiple extraction strategies.

    1. strip markdown code fences (```json ... ```)
    2. ``json.loads`` on the whole text
    3. extract the outermost ``{ ... }`` object, then the outermost ``[ ... ]``
       array

    Returns the parsed object or ``None`` when nothing decodes.
    """
    if not isinstance(text, str) or not text.strip():
        return None
    text = text.strip()

    # Strategy 1: markdown fence wrapper.
    if text.startswith("```"):
        text = "\n".join(
            line for line in text.splitlines()
            if not line.strip().startswith("```")
        ).strip()

    for _name, parse_fn in (
        ("json.loads", lambda t: json.loads(t)),
        ("extract { }",
         lambda t: json.loads(t[t.find("{"):t.rfind("}") + 1]) if "{" in t else None),
        ("extract [ ]",
         lambda t: json.loads(t[t.find("["):t.rfind("]") + 1]) if "[" in t else None),
    ):
        try:
            result = parse_fn(text)
            if result is not None:
                return result
        except (json.JSONDecodeError, ValueError, IndexError):
            continue
    return None


# ---------------------------------------------------------------------------
# Frontmatter parsing (Items 6, 8)
# ---------------------------------------------------------------------------

# Frontmatter = a leading '---' line, a block, then a closing '---' line.
_FRONTMATTER_RE = re.compile(
    r"\A---[ \t]*\n(.*?)\n---[ \t]*(?:\n|\Z)", re.DOTALL
)


def extract_frontmatter(text: str) -> str | None:
    """Return the raw YAML frontmatter block of *text*, or None."""
    if not isinstance(text, str):
        return None
    match = _FRONTMATTER_RE.match(text.lstrip("\ufeff"))
    return match.group(1) if match else None


def parse_frontmatter(fm_text: str) -> tuple[dict | None, str | None]:
    """Parse a frontmatter block. Returns ``(mapping_or_None, error_or_None)``.

    Always uses PyYAML (hard dependency since Item 8). The fragile regex
    fallback has been removed — nested structures and YAML lists are now
    correctly parsed.
    """
    try:
        data = yaml.safe_load(fm_text)
    except yaml.YAMLError as exc:
        return None, f"invalid YAML: {exc}"
    if not isinstance(data, dict):
        return None, "frontmatter is not a YAML mapping"
    return data, None
