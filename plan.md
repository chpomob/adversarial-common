---
spec: "p13-plan-writer-persona"
version: "1.0"
author: "adversarial-plan"
based-on: "adversarial-spec"
findings-input: false
---

# Implementation Plan

## Steps

### P1: Add non-trivial criteria and anti-overload to plan-writer persona
- **Files:** [personas/plan-writer.md]
- **Description:** Insert the addyosmani non-trivial criteria section (branching logic, module/service boundary, compiler/type-system gap, irreversible blast radius) and the anti-overload threshold rule (trivial steps like cosmetic/comment/formatting do not require deep analysis) into the plan-writer persona, after the Rules section and before Self-check.
- **Dependencies:** []
- **Tests:** pytest tests verifying "branching", "boundary", "compiler", "blast radius", and "anti-overload" + "trivial" keywords present in the loaded persona text.
- **Risks:** Insertion point may shift the existing checklist anchors if the file structure changes between spec creation and execution. The "## Self-check before finishing" anchor must remain intact.

### P2: Add caller enumeration table specification to plan-writer persona
- **Files:** [personas/plan-writer.md]
- **Description:** Insert the caller enumeration section requiring a caller table with columns File, Function/Method, Migration Note, plus the rule that unknown callers must be flagged explicitly (never silently omitted), applicable when the plan touches an API change.
- **Dependencies:** [P1]
- **Tests:** pytest tests verifying "caller table", "| File", "Function/Method", "Migration Note" keywords present.
- **Risks:** Must not duplicate or conflict with any pre-existing caller-related text in the persona. Requires careful placement near the non-trivial criteria section added in P1.

### P3: Add full-branch review gate to plan-writer persona
- **Files:** [personas/plan-writer.md]
- **Description:** Insert the full-branch review gate section: a gate step before PR delivery that inspects all changed files together, references pitfall commit `6f0a2c1` (component decomposition ≠ integration), and explains why per-commit reviews miss cross-file bugs by construction.
- **Dependencies:** [P2]
- **Tests:** pytest tests verifying "full-branch review" and "6f0a2c1" keywords present.
- **Risks:** The gate description must not contradict existing pipeline gate rules; the `6f0a2c1` reference must be preserved exactly as specified.

### P4: Create test suite for plan-writer persona additions
- **Files:** [adversarial_common/tests/test_plan_writer_persona.py]
- **Description:** Create a new pytest test file covering all three additions (R1 non-trivial criteria + anti-overload, R2 caller enumeration table column names, R3 full-branch review gate keywords). Use `load_persona("plan-writer")` to load the persona text and assert keyword presence.
- **Dependencies:** [P3]
- **Tests:** The test file itself is the deliverable; run with `python3 -m pytest adversarial_common/tests/test_plan_writer_persona.py -q -p no:cacheprovider`.
- **Risks:** If the persona insertion points change during P1–P3 execution, the test keyword assertions may need minor updates. Tests are pure presence checks so false positives (unrelated usage of keywords) are unlikely.

### P5: Full-suite validation
- **Files:** [adversarial_common/tests/]
- **Description:** Run the full test suite `python3 -m pytest adversarial_common/tests/ -q -p no:cacheprovider` to confirm all existing tests and the new P13 tests pass green.
- **Dependencies:** [P4]
- **Tests:** Full suite execution — all tests must pass.
- **Risks:** Pre-existing test failures unrelated to P13 changes would block this step; if encountered, isolate the failure and report as a pre-existing issue.

## Ordering rationale

P1, P2, P3 modify the same persona file and are ordered to build content cumulatively: non-trivial criteria (P1) provides the foundation for when caller enumeration (P2) is required, and the full-branch review gate (P3) ties them together as the final guardrail. The test file (P4) depends on all persona edits being complete to assert the full set of keywords. Full-suite validation (P5) runs last as the final green check.
