---
name: "readgate-shared-helper"
version: "1.0"
author: "adversarial-spec"
status: "draft"
tags: [adversarial, spec, read-gate]
targets:
  - file: adversarial_common/readgate.py
    description: "New module exposing a validate function and a ReadGatePolicy class implementing the warn→re-run→hard-error state machine."
  - file: adversarial_common/tests/test_readgate.py
    description: "Unit tests covering marker presence, both marker forms, path basename matching, policy transitions, and case-insensitive filename matching."
  - file: adversarial_common/__init__.py
    description: "Re-export readgate public API (validate_agent_output, ReadGatePolicy, ReadGateResult) into the package namespace."
---

# ReadGate shared helper

## Problem

When the pipeline moves working payloads out of agent prompts and into disk
files, nothing prevents a lazy agent from answering without reading the file.
A missed read silently degrades review, spec, plan, and fix quality because
the agent produces output from stale context or guesses. The pipeline needs a
deterministic, testable mechanism to (a) require the agent to emit proof of
reading, (b) warn on a first miss and re-run with a reminder, and (c) fail
with an infrastructure error after repeated misses — never fabricate a
verdict.

## Requirements

- R1: Provide a shared Python module at `adversarial_common/readgate.py`
  exposing a `validate_agent_output` function, a `ReadGatePolicy` class, and
  a `ReadGateResult` type. `validate_agent_output` accepts the agent's raw
  output string and the expected file path, and returns a `ReadGateResult`.
  `ReadGateResult` carries a `marker_found` boolean and a `status` field
  whose possible values are `"pass"`, `"WARNING"`, and `"HARD_ERROR"`.
  `ReadGatePolicy` is a stateful object that tracks consecutive misses per
  path and drives the warn → hard-error escalation.
- R2: The READ marker recognized by `validate_agent_output` MUST accept two
  forms: (a) a literal line matching `READ: <path>` (the keyword `READ:` is
  case-sensitive — `read:` or `Read:` do not match) anywhere in a plain-text
  output, and (b) a `read_files` array containing path strings inside a JSON
  output, scanned at any depth up to 10 nesting levels below the root object.
  If the output is not parseable JSON, only form (a) is attempted and no
  parse error is raised.  Path matching rules: for the plain-text form,
  matching is case-sensitive by exact full path first, falling back to exact
  basename match.  For the JSON `read_files` array form, matching is
  case-insensitive and applied to the filename component (basename) only.
- R3: `ReadGatePolicy` exposes a `check(output: str, path: str) ->
  ReadGateResult` method that performs the marker check with state tracking.
  Transitions are per-path: the first consecutive call where no marker is
  found for a given path returns a result with `status="WARNING"` (the caller
  re-runs with a reminder). The second consecutive miss on the *same* path
  returns a result with `status="HARD_ERROR"` (the caller MUST produce an
  infrastructure failure, exit code != 0, and never emit
  APPROVE/REJECT/REQUEST_CHANGES). A miss against a *different* path does not
  affect the streak for the original path. A present marker resets the miss
  counter for that path to zero and returns `status="pass"`.
- R4: The module is importable as `adversarial_common.readgate` with zero new
  third-party dependencies beyond those already present in
  `adversarial_common` (stdlib + PyYAML). The public API is re-exported from
  `adversarial_common/__init__.py`.
- R5: Unit tests in `adversarial_common/tests/test_readgate.py` cover: marker
  present yields `status="pass"`, both marker forms (plain-text `READ:` line
  and JSON `read_files` array) are recognized independently, basename-only
  match succeeds for plain-text form, case-insensitive basename match
  succeeds for JSON form, warning transition on first miss, hard-error
  transition on second consecutive miss for the same path, counter reset
  after a present marker, and unrelated-path interleaving does not break the
  streak for a different path.

## Acceptance criteria

- AC1 (R1): `adversarial_common/readgate.py` exists, `from
  adversarial_common.readgate import validate_agent_output, ReadGatePolicy,
  ReadGateResult` succeeds with no ImportError.
- AC2 (R2): Passing `"READ: spec.md"` anywhere in a plain-text output to
  `validate_agent_output` with path `spec.md` returns a result with
  `marker_found=True`. Passing `'{"read_files": ["spec.md"]}'` to
  `validate_agent_output` with path `spec.md` returns `marker_found=True`.
- AC3 (R3): For a fresh `ReadGatePolicy`, calling `check(output, path="X")`
  with an output missing the marker returns a result with
  `status="WARNING"`. Calling `check(…)` again with an output still missing
  the marker for the same path `X` returns `status="HARD_ERROR"`. Calling
  `check(…)` with a present marker for path `X` at any point resets the
  counter and returns `status="pass"` + `marker_found=True`. An interleaved
  call for a different path `Y` (missing marker) does NOT escalate path `X`
  to HARD_ERROR.
- AC4 (R4): `python3 -c "import adversarial_common.readgate"` succeeds in a
  fresh Python 3.11+ interpreter with only PyYAML as a pre-installed
  third-party dependency. No new packages are required.
- AC5 (R5): `pytest adversarial_common/tests/test_readgate.py -v` passes
  with tests covering all scenarios enumerated in R5.

## Caller Enumeration

This is a new API with **zero callers** in this repository. The four
consuming repos (`adversarial-spec`, `adversarial-plan`,
`adversarial-code-loop`, `adversarial-code-review`) will become callers once
their own briefs wire the gate into their phase prompts. Search method: `rg
"readgate"` across the repo returns no matches.
