# adversarial-common

Shared engine for multi-persona adversarial pipelines on Hermes Agent / Claude Code / Codex / any LLM CLI.

Powers `adversarial-spec`, `adversarial-plan`, `adversarial-code-loop`, and `adversarial-code-review`.

## What it is

A Python package providing the hardened infra that all adversarial skills share:

- **`runner.py`** — subprocess execution with temp-file IO, process-group kill on timeout, non-UTF-8 tolerance
- **`providers.py`** — CLI provider detection (Claude, Codex, pi, agy, …), persona injection, role-based command resolution
- **`jsonio.py`** — 3-strategy JSON extraction (markdown fences, `{…}` extraction, `[…]` extraction), artifact persistence
- **`gitops.py`** — git workflow: init, branch, commit, diff, stash, squash-merge, tag, reject-marker
- **`snapshot.py`** — dirty-tree baseline for sandbox write detection
- **`personas/`** — 15+ adversarial persona files (builder, critic, fixer, verifier, judge, architect, inspector, synthesis, …)

## Comparison

| Feature | adversarial-common | standalone MAD libs |
|---------|-------------------|-------------------|
| Git-native isolation | ✅ Branch-per-loop | ❌ |
| Multi-provider | ✅ Claude, Codex, pi, agy, … | ❌ One provider |
| Persona injection | ✅ Provider-aware (pi gets tool-based personas) | ❌ |
| JSON robustness | ✅ 3-strategy parser | ❌ Strict `json.loads` |
| Subprocess hardening | ✅ tempfile IO, killpg, UTF-8 replace | ❌ |

## Dependencies

- Python ≥ 3.11
- PyYAML (for frontmatter parsing)
- Git ≥ 2.5 (for worktree support)

## License

MIT
