"""JSON extraction and artifact handling shared by both adversarial pipelines."""

import json
import re
from pathlib import Path


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
