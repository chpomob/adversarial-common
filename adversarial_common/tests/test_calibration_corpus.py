"""Tests for the versioned review-calibration corpus (P18).

Loads every fixture under references/calibration_corpus/ by relative path
and checks the corpus shape the calibration harness depends on: each entry
loads, there are enough entries, and at least one probes prompt injection.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

CORPUS_ROOT = Path(__file__).resolve().parents[2] / "references" / "calibration_corpus"


def _normalize_diff_text(diff_text: str) -> str:
    """Join added/removed/context line bodies into one whitespace-normalized
    string, so a payload that wraps across several diff lines can still be
    matched as a contiguous substring."""
    lines = []
    for line in diff_text.splitlines():
        if line[:1] in ("+", "-", " "):
            line = line[1:]
        lines.append(line.strip())
    return re.sub(r"\s+", " ", " ".join(lines)).strip()


_HUNK_HEADER_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


def _validate_unified_diff(diff_text: str) -> list[str]:
    """Check that every hunk's declared (old_count, new_count) matches the
    number of removed/context and added/context lines actually present in
    its body, the same structural property `git apply --numstat` enforces
    before it will accept a patch as well-formed."""
    errors = []
    lines = diff_text.splitlines()
    i = 0
    hunk_num = 0
    saw_hunk = False
    while i < len(lines):
        match = _HUNK_HEADER_RE.match(lines[i])
        if not match:
            i += 1
            continue
        saw_hunk = True
        hunk_num += 1
        old_count = int(match.group(2) or 1)
        new_count = int(match.group(4) or 1)
        i += 1
        actual_old = 0
        actual_new = 0
        while i < len(lines) and not lines[i].startswith("@@") and not lines[i].startswith(
            "diff --git"
        ):
            line = lines[i]
            if line == "" or line[0] == " ":
                actual_old += 1
                actual_new += 1
            elif line[0] == "-":
                actual_old += 1
            elif line[0] == "+":
                actual_new += 1
            elif line[0] == "\\":
                pass
            else:
                errors.append(f"hunk {hunk_num}: unexpected line prefix {line!r}")
            i += 1
        if (actual_old, actual_new) != (old_count, new_count):
            errors.append(
                f"hunk {hunk_num}: header declares (-{old_count},+{new_count}) but body "
                f"contains (-{actual_old},+{actual_new})"
            )
    if not saw_hunk:
        errors.append("no '@@ ... @@' hunk header found")
    return errors


def _reconstruct_preimage(diff_text: str) -> tuple[str, str]:
    """Rebuild the pre-patch file path and content implied by a diff's own
    old-side (context/removed) lines, padding any gap the diff doesn't show
    with placeholder lines so `git apply` has a real file to match against."""
    file_match = re.search(r"^--- a/(\S+)", diff_text, re.M)
    assert file_match, "diff.txt missing '--- a/<path>' header"
    path = file_match.group(1)

    old_lines: dict[int, str] = {}
    lines = diff_text.splitlines()
    i = 0
    while i < len(lines):
        match = _HUNK_HEADER_RE.match(lines[i])
        if not match:
            i += 1
            continue
        old_line_no = int(match.group(1))
        i += 1
        while i < len(lines) and not lines[i].startswith("@@") and not lines[i].startswith(
            "diff --git"
        ):
            line = lines[i]
            if line == "" or line[0] == " ":
                old_lines[old_line_no] = line[1:] if line[:1] == " " else line
                old_line_no += 1
            elif line[0] == "-":
                old_lines[old_line_no] = line[1:]
                old_line_no += 1
            i += 1
    max_line = max(old_lines) if old_lines else 0
    content = "\n".join(
        old_lines.get(n, f"__PLACEHOLDER_LINE_{n}__") for n in range(1, max_line + 1)
    )
    return path, content + "\n"


def _load_corpus() -> dict[str, dict]:
    entries = {}
    for child in sorted(CORPUS_ROOT.iterdir()):
        if not child.is_dir():
            continue
        diff_path = child / "diff.txt"
        gold_path = child / "gold.json"
        if not (diff_path.is_file() and gold_path.is_file()):
            continue
        diff_text = diff_path.read_text()
        gold = json.loads(gold_path.read_text())
        entries[child.name] = {"diff": diff_text, "gold": gold}
    return entries


def test_corpus_loads_all_diffs():
    entries = _load_corpus()

    assert entries, "expected at least one calibration fixture"
    for fixture_id, entry in entries.items():
        assert entry["diff"].strip(), f"{fixture_id}: diff.txt is empty"
        hunk_errors = _validate_unified_diff(entry["diff"])
        assert not hunk_errors, (
            f"{fixture_id}: diff.txt is not a well-formed unified diff: {hunk_errors}"
        )
        gold = entry["gold"]
        assert isinstance(gold, dict), f"{fixture_id}: gold.json must be a JSON object"
        findings = gold.get("findings")
        assert isinstance(findings, list), f"{fixture_id}: gold.json missing 'findings' list"
        for finding in findings:
            for field in ("id", "severity", "file", "summary"):
                assert field in finding, (
                    f"{fixture_id}: finding {finding.get('id', '?')} missing '{field}'"
                )
            assert finding["severity"] in {"blocker", "major", "minor", "nit"}, (
                f"{fixture_id}: finding {finding['id']} has unknown severity "
                f"{finding['severity']!r}"
            )


def test_corpus_has_minimum_entries():
    entries = _load_corpus()

    assert 10 <= len(entries) <= 15, (
        f"expected 10-15 calibration fixtures, found {len(entries)}: {sorted(entries)}"
    )


def test_corpus_has_injection_probe():
    entries = _load_corpus()

    probes = [
        (fixture_id, entry)
        for fixture_id, entry in entries.items()
        if entry["gold"].get("kind") == "prompt_injection_probe"
    ]
    assert probes, "expected at least one fixture with gold.json kind == 'prompt_injection_probe'"

    for fixture_id, entry in probes:
        gold = entry["gold"]
        payload = gold.get("injection_payload")
        assert payload, f"{fixture_id}: gold.json missing non-empty 'injection_payload'"
        normalized_payload = re.sub(r"\s+", " ", payload).strip()
        assert normalized_payload in _normalize_diff_text(entry["diff"]), (
            f"{fixture_id}: injection_payload does not appear (whitespace-normalized) in diff.txt"
        )
        assert any(
            finding.get("category") == "prompt_injection"
            for finding in gold.get("findings", [])
        ), f"{fixture_id}: expected a finding with category 'prompt_injection'"


def test_corpus_has_paired_attribution_fixture():
    entries = _load_corpus()

    paired = [
        (fixture_id, entry)
        for fixture_id, entry in entries.items()
        if entry["gold"].get("kind") == "paired_attribution"
    ]
    assert len(paired) >= 2, "expected a paired-attribution fixture (2 linked entries)"

    pair_ids = {entry["gold"].get("pair_id") for _, entry in paired}
    assert None not in pair_ids, "paired-attribution fixtures must share a pair_id"
    for pair_id in pair_ids:
        members = [e for _, e in paired if e["gold"].get("pair_id") == pair_id]
        assert len(members) == 2, f"pair_id {pair_id!r} must link exactly 2 fixtures"


def test_corpus_has_paired_preference_fixture():
    entries = _load_corpus()

    paired = [
        (fixture_id, entry)
        for fixture_id, entry in entries.items()
        if entry["gold"].get("kind") == "paired_preference"
    ]
    assert len(paired) >= 2, "expected a paired-preference fixture (2 linked entries)"

    pair_ids = {entry["gold"].get("pair_id") for _, entry in paired}
    assert None not in pair_ids, "paired-preference fixtures must share a pair_id"
    for pair_id in pair_ids:
        members = [e for _, e in paired if e["gold"].get("pair_id") == pair_id]
        assert len(members) == 2, f"pair_id {pair_id!r} must link exactly 2 fixtures"
        ranks = sorted(m["gold"].get("preference_rank") for m in members)
        assert ranks == [1, 2], (
            f"pair_id {pair_id!r} must have preference_rank 1 and 2, got {ranks}"
        )


@pytest.mark.skipif(shutil.which("git") is None, reason="git is not on PATH")
def test_corpus_diffs_apply_with_git():
    """Every fixture diff must be consumable by `git apply`, not just pass
    the header/body count check: reconstruct the pre-patch file each diff
    implies and confirm `git apply --check` accepts it as a valid patch."""
    entries = _load_corpus()

    for fixture_id, entry in entries.items():
        diff_text = entry["diff"]
        rel_path, content = _reconstruct_preimage(diff_text)

        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_root = Path(tmpdir)
            file_path = tmp_root / rel_path
            file_path.parent.mkdir(parents=True, exist_ok=True)
            file_path.write_text(content)

            subprocess.run(["git", "init", "-q"], cwd=tmp_root, check=True)
            subprocess.run(
                ["git", "-c", "user.email=t@example.com", "-c", "user.name=t", "add", "."],
                cwd=tmp_root,
                check=True,
            )
            subprocess.run(
                [
                    "git",
                    "-c",
                    "user.email=t@example.com",
                    "-c",
                    "user.name=t",
                    "commit",
                    "-q",
                    "-m",
                    "init",
                ],
                cwd=tmp_root,
                check=True,
            )
            diff_path = tmp_root / "fixture.diff"
            diff_path.write_text(diff_text)

            result = subprocess.run(
                ["git", "apply", "--check", "fixture.diff"],
                cwd=tmp_root,
                capture_output=True,
                text=True,
            )
            assert result.returncode == 0, (
                f"{fixture_id}: diff.txt rejected by `git apply --check`: "
                f"{result.stderr.strip()}"
            )
