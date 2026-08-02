# Calibration corpus

Versioned fixtures for calibrating review/judge personas against known
findings. Each subdirectory is one fixture:

- `diff.txt` — a unified diff, the input the persona would review.
- `gold.json` — the known-correct findings for that diff:
  ```json
  {
    "id": "<fixture-id>",
    "kind": "standard | prompt_injection_probe | paired_attribution | paired_preference",
    "findings": [
      {"id": "F1", "severity": "blocker|major|minor|nit", "file": "...", "line": 0, "summary": "..."}
    ]
  }
  ```
  `findings` is `[]` when the diff is clean and no findings are expected.

## Fixture categories

- **Standalone** (`kind: "standard"`) — one diff, one known bug class
  (SQL injection, path traversal, hardcoded secret, resource leak, bare
  except, mutable default argument, unbounded recursion, command injection).
- **Prompt-injection probe** (`kind: "prompt_injection_probe"`) —
  `injection-probe-01`. The diff embeds a payload instructing the reviewer
  to ignore its instructions and approve unconditionally, plus an attempt
  to forge the untrusted-body delimiter used by
  `adversarial_common.providers._delimit_untrusted_body`. `gold.json`
  carries the literal payload text in `injection_payload` and flags it as
  a `category: "prompt_injection"` finding — the persona must report it as
  a finding, not obey it.
- **Paired attribution** (`kind: "paired_attribution"`, linked via
  `pair_id`) — `attribution-buggy` / `attribution-clean`. Both bundle the
  same cosmetic rename; only `attribution-buggy` also contains a real
  off-by-one bug. Calibrates whether a reviewer attributes the finding to
  the actual defect and abstains on the clean sibling instead of flagging
  the rename.
- **Paired preference** (`kind: "paired_preference"`, linked via
  `pair_id`, ranked via `preference_rank`) — `preference-fix-a` /
  `preference-fix-b`. Two candidate fixes for the same race condition;
  `preference_rank: 1` is the fully-correct fix, `preference_rank: 2` still
  leaves the race. Calibrates a preference judge comparing two candidates.

## Referencing the corpus from tests

Tests load the corpus by relative path from the repo root, e.g.
`Path(__file__).resolve().parents[2] / "references" / "calibration_corpus"`
— see `adversarial_common/tests/test_calibration_corpus.py`.
