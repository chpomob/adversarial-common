"""JSON extraction, finding normalization, and artifact handling."""

import json
import re
from pathlib import Path

import yaml

from .gates import TRUNCATION_MARKER


VALID_CONFIDENCE = frozenset({"high", "medium", "low"})
VALID_BASIS = frozenset({"spec", "code", "inference", "external"})


def strip_json_wrapper(text):
    """Strip markdown fences/prose and return the largest JSON object text."""
    if not text:
        return text
    cleaned = re.sub(r"```(?:json)?\s*\n?", "", text)
    cleaned = re.sub(r"\n?```", "", cleaned)
    candidates = []
    pos = 0
    decoder = json.JSONDecoder()
    while pos < len(cleaned):
        brace = cleaned.find("{", pos)
        if brace == -1:
            break
        try:
            obj, end = decoder.raw_decode(cleaned[brace:])
            candidates.append(json.dumps(obj))
            pos = brace + end
        except json.JSONDecodeError:
            pos = brace + 1
    return max(candidates, key=len) if candidates else text


def save_artifact(out_dir, name, content):
    """Write an artifact under ``out_dir``, creating parents as needed."""
    path = Path(out_dir) / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    return path


def resume_artifact(out_dir, filename, label):
    """Return a saved non-empty artifact for a resumed phase."""
    path = Path(out_dir) / filename
    if path.is_file():
        text = path.read_text()
        if text.strip():
            print(f"  >> Resuming '{label}' from existing {filename}")
            return text
    return None


def write_final_json(out_dir, verdict, **extra):
    """Write the authoritative machine-readable final verdict artifact."""
    payload = {"verdict": verdict}
    payload.update(extra)
    return save_artifact(out_dir, "final.json", json.dumps(payload, indent=2) + "\n")


def parse_json_output(text: str, warnings: list | None = None) -> dict | list | None:
    """Parse model JSON, rejecting truncated output and normalizing findings."""
    if not isinstance(text, str) or not text.strip():
        return None
    if TRUNCATION_MARKER in text:
        if warnings is not None:
            warnings.append({
                "code": "truncated_json_output",
                "message": "provider output was truncated before JSON parsing",
            })
        return None
    candidate = text.strip()
    if candidate.startswith("```"):
        candidate = "\n".join(
            line for line in candidate.splitlines()
            if not line.strip().startswith("```")
        ).strip()
    result = None
    for parse_fn in (
        lambda value: json.loads(value),
        lambda value: json.loads(value[value.find("{"):value.rfind("}") + 1])
        if "{" in value else None,
        lambda value: json.loads(value[value.find("["):value.rfind("]") + 1])
        if "[" in value else None,
    ):
        try:
            result = parse_fn(candidate)
            if result is not None:
                break
        except (json.JSONDecodeError, ValueError, IndexError):
            continue
    if result is None:
        return None
    return normalize_findings(result, warnings=warnings)


def normalize_findings(payload, warnings: list | None = None):
    """Normalize finding epistemic labels in place without dropping findings."""
    root = payload if isinstance(payload, dict) else None
    if isinstance(payload, dict):
        findings = payload.get("findings")
        if not isinstance(findings, list):
            return payload
    elif isinstance(payload, list):
        findings = payload
    else:
        return payload
    recorded = warnings if warnings is not None else []
    for index, finding in enumerate(findings):
        if not isinstance(finding, dict):
            continue
        confidence = finding.get("confidence")
        basis = finding.get("basis")
        if confidence not in VALID_CONFIDENCE or basis not in VALID_BASIS:
            finding["confidence"] = "low"
            finding["basis"] = "inference"
            warning = {
                "code": "epistemic_label_defaulted",
                "finding_id": str(finding.get("id", index)),
                "message": "missing or invalid confidence/basis normalized to low/inference",
            }
            recorded.append(warning)
            finding.setdefault("warnings", []).append(warning["code"])
    if root is not None and recorded:
        root_warnings = root.setdefault("warnings", [])
        for warning in recorded:
            if warning not in root_warnings:
                root_warnings.append(warning)
    return payload


def epistemic_distribution(findings) -> dict:
    """Count confidence, basis, and combined labels for synthesis/final JSON."""
    distribution = {
        "confidence": {name: 0 for name in sorted(VALID_CONFIDENCE)},
        "basis": {name: 0 for name in sorted(VALID_BASIS)},
        "combined": {},
    }
    normalized = normalize_findings(list(findings))
    for finding in normalized:
        if not isinstance(finding, dict):
            continue
        confidence = finding["confidence"]
        basis = finding["basis"]
        distribution["confidence"][confidence] += 1
        distribution["basis"][basis] += 1
        key = f"{confidence}/{basis}"
        distribution["combined"][key] = distribution["combined"].get(key, 0) + 1
    return distribution


_FRONTMATTER_RE = re.compile(r"\A---[ \t]*\n(.*?)\n---[ \t]*(?:\n|\Z)", re.DOTALL)


def extract_frontmatter(text: str) -> str | None:
    """Return the raw YAML frontmatter block, if present."""
    if not isinstance(text, str):
        return None
    match = _FRONTMATTER_RE.match(text.lstrip("\ufeff"))
    return match.group(1) if match else None


def parse_frontmatter(fm_text: str) -> tuple[dict | None, str | None]:
    """Parse YAML frontmatter into ``(mapping, error)``."""
    try:
        data = yaml.safe_load(fm_text)
    except yaml.YAMLError as exc:
        return None, f"invalid YAML: {exc}"
    if not isinstance(data, dict):
        return None, "frontmatter is not a YAML mapping"
    return data, None


__all__ = [
    "VALID_BASIS", "VALID_CONFIDENCE", "epistemic_distribution",
    "extract_frontmatter", "normalize_findings", "parse_frontmatter",
    "parse_json_output", "resume_artifact", "save_artifact",
    "strip_json_wrapper", "write_final_json",
]
