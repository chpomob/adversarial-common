---
spec: "readgate-shared-helper"
version: "1.0"
author: "adversarial-plan"
based-on: "adversarial-spec"
findings-input: true
---

# Implementation Plan

## Steps

### P1: ReadGateResult and validate_agent_output — stateless marker detection
- **Files:** [adversarial_common/readgate.py]
- **Description:** Create the new module with a `ReadGateResult` dataclass (fields: `marker_found: bool`, `status: str` where status ∈ `{"pass", "WARNING", "HARD_ERROR"}`) and a `validate_agent_output(output: str, path: str) -> ReadGateResult` function. The function implements two marker detection strategies: (a) plain-text `READ: <path>` line matching — case-sensitive keyword, exact full-path match with basename fallback; (b) JSON `read_files` array scanning up to 10 nesting levels — case-insensitive basename match. JSON parsing failure degrades gracefully to plain-text only. No state tracking in this step (stateless, always returns `status="pass"` when found, `status="WARNING"` when not — the policy layer in P2 will override status). All public functions and the dataclass carry docstrings. Covers R1 (ReadGateResult and validate_agent_output) and R2 (both marker forms, path matching rules).
- **Dependencies:** []
- **Tests:** Manual ad-hoc in-file smoke test (`if __name__ == "__main__":`) covering: plain-text marker found, JSON marker found, plain-text basename-only match, JSON case-insensitive basename match, missing marker returns marker_found=False, garbage JSON degrades to plain-text scan. Formal pytest tests deferred to P4.
- **Risks:** JSON deep-scan performance on deeply nested output (depth limit 10 mitigates); `os.path.basename` behavior on paths with trailing slashes — test explicitly for edge case; regex for `READ:` line must anchor at both line start (`^`) and line end (`$`) — a prefix-only match would let `READ: spec.md.bak` or `READ: spec.md but I skimmed it` pass, defeating the gate (finding P1).

### P2: ReadGatePolicy — stateful warn→hard-error escalation
- **Files:** [adversarial_common/readgate.py]
- **Description:** Add `ReadGatePolicy` class to `readgate.py`. It maintains a `_misses: dict[str, int]` tracking consecutive misses per path. The `check(output: str, path: str) -> ReadGateResult` method delegates to `validate_agent_output` for marker detection, then applies state transitions: if marker found → reset `_misses[path] = 0`, return `status="pass"`; if no marker → increment `_misses[path]`, return `status="WARNING"` on first miss, `status="HARD_ERROR"` on second consecutive miss. Calls for a different path do not affect the streak of the original path. The class and its public methods carry docstrings. Covers R1 (ReadGatePolicy) and R3 (all transitions).
- **Dependencies:** [P1]
- **Tests:** In-file smoke test extending P1's `__main__` block: fresh policy returns WARNING on first miss, HARD_ERROR on second miss same path, interleaved different-path miss does NOT escalate original, marker resets counter to zero. Formal tests in P4.
- **Risks:** Thread safety — `_misses` dict is not locked; if called concurrently the counter may be inaccurate. Mitigation: document single-threaded assumption in docstring; add lock later if concurrent callers emerge. Key collision if path objects are used as dict keys — ensure `str`-only keys in type annotation.

### P3: Package re-exports from adversarial_common/__init__.py
- **Files:** [adversarial_common/__init__.py]
- **Description:** Add `ReadGateResult`, `ReadGatePolicy`, and `validate_agent_output` to the `__all__` list and import them from `.readgate`. Export block follows existing pattern: import line at top of imports section, entries in `__all__`. No other files touched. Covers R1 (public API reachable from package root) and R4 (importable as `adversarial_common.readgate` and from `adversarial_common`).
- **Dependencies:** [P1, P2]
- **Tests:** `python3 -c "from adversarial_common.readgate import validate_agent_output, ReadGatePolicy, ReadGateResult"` and `python3 -c "from adversarial_common import validate_agent_output, ReadGatePolicy, ReadGateResult"` — both must succeed with no ImportError. Automatically verified by the smoke tests and formal test imports in P4.
- **Risks:** Name collision with existing `__all__` entries — verify no existing symbol named `validate_agent_output`, `ReadGatePolicy`, or `ReadGateResult` in the current `__init__.py` (they are not present). Circular import if `readgate.py` imports from `adversarial_common` — avoid by using only stdlib imports in `readgate.py`.

### P4: Unit tests for marker detection forms and policy state machine
- **Files:** [adversarial_common/tests/test_readgate.py]
- **Description:** Write pytest test suite using the existing project patterns (no fixtures framework, plain `def test_*` functions). Cover all scenarios from R5: (1) `test_marker_present_returns_pass` — plain-text `READ:` line found, `status="pass"`; (2) `test_json_read_files_array_found` — JSON `read_files` array with matching path; (3) `test_basename_only_plain_text_match` — basename match succeeds when full path differs; (4) `test_case_insensitive_json_basename_match` — case-insensitive basename in JSON form; (5) `test_warning_on_first_miss` — policy returns WARNING on first miss for a path; (6) `test_hard_error_on_second_miss` — second consecutive miss escalates to HARD_ERROR; (7) `test_marker_resets_counter` — present marker after a miss resets to pass; (8) `test_unrelated_path_interleaving` — miss on path Y does not affect streak on path X; (9) `test_trailing_content_after_read_path` — READ: line with trailing chars after the path (e.g. `READ: spec.md.bak`) does NOT match on full-path check, verifying end-of-line anchoring (finding P1); (10) `test_nested_read_files_at_depth` — JSON `read_files` array nested inside a sub-object at depth 2+ (e.g. `{"data": {"read_files": ["spec.md"]}}`) is still found, covering the R2 depth-scanning requirement (finding P2). Covers R5 and AC5. Also implicitly validates AC2 and AC3 through test assertions.
- **Dependencies:** [P1, P2, P3]
- **Tests:** The test file itself is the deliverable; verify with `pytest adversarial_common/tests/test_readgate.py -v`. No separate test harness needed.
- **Risks:** Test ordering dependency — all tests must be independent (policy tests use fresh instances). JSON edge case: `read_files` nested inside a non-array JSON value (e.g., `{"data": {"read_files": "spec.md"}}`) — scanner must handle strings inside `read_files` that are not arrays; add explicit test for non-array `read_files` value being skipped.

### P5: Full-branch review gate
- **Files:** [adversarial_common/readgate.py, adversarial_common/__init__.py, adversarial_common/tests/test_readgate.py]
- **Description:** After all implementation and tests pass, perform a full-branch review across all three changed files. Per-commit reviews miss cross-file bugs by construction: P1+P2 in `readgate.py` and P3 in `__init__.py` may each be correct in isolation but a mismatch in exported names or import paths creates a broken state only visible when inspecting both together. The review checks: (a) import path in `__init__.py` matches the actual symbols defined in `readgate.py`; (b) `__all__` entries match re-exported names; (c) test imports in `test_readgate.py` resolve correctly against the re-exports; (d) no leftover debug print/breakpoint in production code; (e) docstrings exist on all public symbols; (f) the `ReadGateResult.status` field only ever contains one of the three valid literals. This is a non-code gate step: run `pytest adversarial_common/tests/test_readgate.py -v`, the import checks from P3, and manual inspection of the diff.
- **Dependencies:** [P4]
- **Tests:** No new test file; the gate is validated by re-running P4's test suite and P3's import checks against the final state of all three files. A reviewer confirms all checks pass before merge.
- **Risks:** Human error in manual inspection — mitigated by the automated pytest run catching import mismatches and test failures. The gate is a checklist, not automated enforcement.

## Ordering rationale

P1 before P2: `ReadGateResult` and `validate_agent_output` are the stateless foundation; `ReadGatePolicy.check()` delegates to `validate_agent_output` and wraps its result with state transitions. Building the policy first would require stubbing the detection logic.

P1+P2 before P3: The `__init__.py` re-exports symbols that must already exist in `readgate.py`. Importing from a module that hasn't been written yet would fail import checks.

P3 before P4: The test file imports from `adversarial_common.readgate` and optionally from `adversarial_common` directly. Both import paths must resolve, which requires the re-exports in place. In practice P4 could import directly from `adversarial_common.readgate` without the package re-exports, but the spec requires AC1 verifies both import paths — the test file should exercise the same paths.

P4 before P5: The full-branch review gate runs the test suite and inspects the final state of all files. It cannot run before the files and tests exist.

## Caller enumeration

This is a new API with **zero callers** in this repository. The four consuming repos (`adversarial-spec`, `adversarial-plan`, `adversarial-code-loop`, `adversarial-code-review`) will become callers once their own briefs wire the gate into their phase prompts. `rg "readgate"` across the repo returns no matches. No migration table needed.

## Self-check checklist

- [x] Every step has Files, Description, Dependencies, Tests, Risks.
- [x] Every step's Files lists concrete real paths — no guesses or placeholders.
- [x] Frontmatter `spec:` matches the `name:` of the spec in `spec.md` (`readgate-shared-helper`).
- [x] No circular dependencies; no step depends on a later step (P1→P2→P3→P4→P5, all forward).
- [x] Every spec requirement maps to at least one step: R1→P1,P2,P3; R2→P1; R3→P2; R4→P3; R5→P4.
- [x] `findings-input: true` — findings from adversarial-review addressed in plan (P1 risk note, P4 tests 9-10, P1/P2 docstring delivery).
- [x] A full-branch review gate step is present (P5).
- [x] Ordering rationale explains the chosen sequence.
- [x] `plan.md` exists on disk with valid frontmatter.
