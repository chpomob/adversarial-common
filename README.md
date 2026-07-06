# adversarial-common

Shared engine for the `adversarial-code-loop` and `adversarial-code-review`
skills. Not a standalone skill — a library imported by both scripts via:

```python
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', 'adversarial-common'))
from adversarial_common import runner, jsonio, providers, snapshot
```

## Contents

- `adversarial_common/runner.py` — hardened `run_cli()` (Popen + temp-file IO +
  `start_new_session` + killpg on timeout) and `fail_phase()`
- `adversarial_common/jsonio.py` — `strip_json_wrapper()` (largest-valid-JSON
  extraction), `save_artifact()`, `resume_artifact()`, `write_final_json()`
- `adversarial_common/providers.py` — `detect_provider()`, `inject_persona()`,
  `enhance_cmd_for_project()`, `resolve_role_cmd()` (flag > env > default),
  `default_wrapper_cmd()`
- `adversarial_common/snapshot.py` — `snapshot_workdir()` git baseline
- `personas/` — single source of truth for all role personas (English):
  builder, critic, fixer, verifier, judge (loop) · architect, inspector,
  cross_review, synthesis (review)

Any fix to subprocess handling, JSON extraction, or provider behavior lands
here once and benefits both pipelines — this replaces the old
"mutualization checklist" process of manually porting fixes between the two
scripts.
