"""JSON extraction, finding normalization, and artifact handling."""

import json
import re
from collections.abc import Iterable
from pathlib import Path
from typing import Any, Final

import yaml

from .gates import TRUNCATION_MARKER


CONFIDENCE_LEVELS: Final = ("high", "medium", "low")
BASIS_TYPES: Final = ("spec", "code", "inference", "external")
VALID_CONFIDENCE: Final = frozenset(CONFIDENCE_LEVELS)
VALID_BASIS: Final = frozenset(BASIS_TYPES)

_EPISTEMIC_WARNING_CODE: Final = "epistemic_label_defaulted"
_TRUNCATION_SUFFIX_RE: Final = re.compile(
    rf"{re.escape(TRUNCATION_MARKER.rstrip())}(?:\s*```)?\s*\Z"
)


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

    candidate = text.strip()
    if candidate.startswith("```"):
        candidate = "\n".join(
            line for line in candidate.splitlines()
            if not line.strip().startswith("```")
        ).strip()

    try:
        result = json.loads(candidate)
    except (json.JSONDecodeError, ValueError):
        if _TRUNCATION_SUFFIX_RE.search(text):
            if warnings is not None:
                warnings.append({
                    "code": "truncated_json_output",
                    "message": "provider output was truncated before JSON parsing",
                })
            return None
        result = _extract_largest_json(candidate)
    if result is None:
        return None
    return normalize_findings(result, warnings=warnings)


def _extract_largest_json(text: str) -> Any | None:
    """Return the largest independently decodable JSON object or array.

    ``raw_decode`` stops at the end of a value, so trailing prose or stray
    braces cannot corrupt an otherwise complete reviewer payload.
    """
    decoder = json.JSONDecoder()
    candidates: list[tuple[int, int, Any]] = []
    for start, character in enumerate(text):
        if character not in "{[":
            continue
        try:
            value, end = decoder.raw_decode(text[start:])
        except json.JSONDecodeError:
            continue
        candidates.append((end, -start, value))
    if not candidates:
        return None
    return max(candidates, key=lambda candidate: candidate[:2])[2]


def normalize_findings(payload: Any, warnings: list | None = None) -> Any:
    """Normalize finding epistemic labels in place without dropping findings.

    Only the two label fields and warning metadata are changed.  All source
    metadata, in particular an ``origin`` marker, remains untouched.
    """
    root = payload if isinstance(payload, dict) else None
    if isinstance(payload, dict):
        findings = payload.get("findings")
        if not isinstance(findings, list):
            return payload
    elif isinstance(payload, list):
        findings = payload
    else:
        return payload

    normalization_warnings: list[dict[str, str]] = []
    for index, finding in enumerate(findings):
        if not isinstance(finding, dict):
            continue
        confidence = finding.get("confidence")
        basis = finding.get("basis")
        if confidence not in VALID_CONFIDENCE or basis not in VALID_BASIS:
            finding["confidence"] = "low"
            finding["basis"] = "inference"
            warning = {
                "code": _EPISTEMIC_WARNING_CODE,
                "finding_id": str(finding.get("id", index)),
                "message": "missing or invalid confidence/basis normalized to low/inference",
            }
            normalization_warnings.append(warning)
            if warnings is not None:
                warnings.append(warning)
            _append_finding_warning(finding, warning["code"])

    if root is not None and normalization_warnings:
        _append_root_warnings(root, normalization_warnings)
    return payload


def _append_finding_warning(finding: dict, code: str) -> None:
    """Attach a warning code while tolerating malformed model metadata."""
    current = finding.get("warnings")
    if isinstance(current, list):
        if code not in current:
            current.append(code)
        return
    finding["warnings"] = [code] if current is None else [current, code]


def _append_root_warnings(root: dict, new_warnings: list[dict[str, str]]) -> None:
    """Record normalization warnings on an object payload."""
    current = root.get("warnings")
    if not isinstance(current, list):
        current = [] if current is None else [current]
        root["warnings"] = current
    for warning in new_warnings:
        if warning not in current:
            current.append(warning)


def epistemic_distribution(findings: Iterable[Any]) -> dict[str, dict]:
    """Count normalized epistemic labels without mutating caller-owned findings."""
    distribution = {
        "confidence": {name: 0 for name in CONFIDENCE_LEVELS},
        "basis": {name: 0 for name in BASIS_TYPES},
        "combined": {},
    }
    for finding in findings:
        if not isinstance(finding, dict):
            continue
        confidence = finding.get("confidence")
        basis = finding.get("basis")
        if confidence not in VALID_CONFIDENCE or basis not in VALID_BASIS:
            confidence = "low"
            basis = "inference"
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
