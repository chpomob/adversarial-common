# adversarial-common — Synthesis Review

**Sources collapsed:** Architect (A1–A10) · Inspector (B1–B8) · Cross-Review 1 (X1–X11) · Cross-Review 2 (X1–X9)
**Verification:** every finding below was checked against the source on disk; reproducible claims were re-run (`2 of 27 tests fail`). Status tags: ✅ reproduced / ✅ test fails · 🔁 validated by both cross-reviews · 📝 code-confirmed, single-source · ⚠️ single-source, not independently verified.

---

## Verdict

**REQUEST_CHANGES**

Not APPROVE: the suite ships two failing tests (`test_squash_merge`, `test_auto_init_sets_identity`), and the self-described "hardened" `run_cli` reproducibly crashes on a single non-UTF-8 byte — both are objective, not opinion. Not REJECT: the architecture is sound and every defect is a localized fix; nothing requires a rewrite.

---

## Summary

`adversarial-common` is a shared library backing two pipelines (adversarial-loop, adversarial-code-review). The review surfaced one systemic problem and a cluster of implementation defects. The systemic problem is **A1**: the shared personas (`personas/architect.md`, `inspector.md`, `cross_review.md`) hardcode a specific RF project — ESP32-S3, CC1101 at 433 MHz, Goertzel/Kalman, RSSI quantization, BLE spectral scan, WiFi CSI. Because the README declares `personas/` the "single source of truth for all role personas" of a *reusable engine*, every consumer reviewing non-RF code (including this very Python repo) gets reviewers primed to hunt for DSP/SNR/antenna issues that cannot exist in the diff — a recipe for hallucinated findings. Domain context belongs in per-project injection, not the shared library.

The implementation defects concentrate in three files. **`gitops.squash_merge`** is the worst: four distinct defects, two of which the project's own tests catch (single-commit fast-forwards instead of squashing; tree-neutral histories hit a guaranteed `nothing to commit` failure). **`runner.run_cli`** violates its "hardened" contract — strict UTF-8 decoding crashes on any invalid byte the child emits (reproduced), and there is no output cap so a noisy child can fill the disk over the 600 s timeout. **`snapshot.snapshot_workdir`** silently degrades on Git worktrees and nested paths (`.git` is a file there, not a directory) — exactly the case it exists to handle — and its path-set model is *structurally* incapable of attributing a fixer's re-edit of an already-dirty file.

Two cross-review challenges were resolved in the consumer's favor: the `jsonio` path-traversal finding (B6) is a real containment defect but has no attacker-controlled filename reaching it (only the constant `final.json`), so it drops from major to minor defense-in-depth; the multi-brace JSON extraction (B7) genuinely returns `None`, but returning `None` on ambiguous input is defensible, so it stays minor. The provider-detection finding (A5/B5) goes the other way: the Architect called it minor, but validated reproduction shows it injects Claude-only flags into unrelated commands, so it is major.

---

## Critical Findings

### C1 — `squash_merge` has four distinct defects  🔁 ✅
`adversarial_common/gitops.py:248` (`def squash_merge`)

| # | Defect | Status | Lines |
|---|--------|--------|-------|
| a | **Single-commit sources fast-forward, discarding `message`.** `count==1` → `merge --ff-only`; HEAD becomes the source commit with its *original* message. `test_squash_merge` asserts `'squashed feat'` and gets `'loop change'`. The in-code comment claiming `--squash` "would produce an empty tree" for a descendant is **wrong** — `git merge --squash` stages the full target→source diff correctly. | ✅ test fails | 271–276 |
| b | **Tree-neutral histories (2+ commits, net-zero diff) hit a guaranteed commit failure.** `merge --squash` succeeds but stages nothing; the unconditional `commit -m message` (no `--allow-empty`) exits nonzero. Reachable in practice because `commit_all` deliberately creates empty commits. | 🔁 reproduced (two empty source commits) | 277–285 |
| c | **Already-merged source (`merge_base == source_head`) deletes the branch and returns without committing**, silently discarding the caller's `message`. Callers that tag evidence against the resulting merge commit record wrong history (no commit was made). | 📝 | 262–264 |
| d | **Conflict leaves a corrupted index.** On `merge --squash` conflict the function raises `GitError` with no `git merge --abort` / `git reset --merge`; the target branch stays checked out with a conflicted index, so every subsequent `commit_all`/`is_dirty`/`checkout` operates on broken state. | 📝 | 278–282 |

**Risk:** confirmed (a ✅ test, b 🔁 reproduced, c/d 📝 code-confirmed). Merge semantics must not depend on an internal commit count invisible to the caller.

### C2 — "Hardened" `run_cli` crashes on a single non-UTF-8 byte  🔁 ✅
`adversarial_common/runner.py:45-46, 68, 71`

`out_f`/`err_f` are opened `encoding="utf-8"` (strict). Any child emitting one invalid byte — binary noise, locale-encoded diagnostics, or a multibyte sequence truncated when `SIGKILL` lands mid-write on the **timeout path** — makes `out_f.read()` raise `UnicodeDecodeError`, uncaught, aborting the whole pipeline instead of returning the documented `(stdout, stderr, rc)` tuple. Reproduced with a child writing `0xff`. **Fix:** open with `errors="replace"` (or decode defensively at the process boundary).

**Risk:** confirmed by reproduction. This is the highest-trust boundary in the library and it is the most easily reachable crash.

### C3 — `snapshot_workdir` is blind to worktrees, nested paths, and re-edits  🔁 ✅
`adversarial_common/snapshot.py:11, 18`

Two compounding defects, both undermining the snapshot's sole purpose ("only attribute NEW disk changes to the fixer"):
- **Repository gate is wrong.** `os.path.isdir(workdir + "/.git")` is false for linked worktrees (`.git` is a file) and for a workdir nested below the repo root (no `.git` at all). The function silently returns `None`, disabling the dirty-file baseline. Reproduced for both cases. The module *already has* `detect_enclosing_repo()` using `git rev-parse` for exactly this reason — `snapshot_workdir` uses the weaker test. The legacy fixer fallback repeats the same gate.
- **Path-set model is structurally insufficient.** The snapshot records only dirty *path names* (`line[3:]`), not content. If a file was dirty at startup and the fixer edits it again, both snapshots contain the same path and a set-difference cannot see the new change. The stated "since-this-snapshot" guarantee is **algorithmically impossible** without hashes/patches/tree baseline.

Additionally (part of A6), porcelain rename lines are stored as the literal `"old -> new"` and quoted paths keep their quotes, so those entries never match later path comparisons.

**Risk:** worktree gate = confirmed by reproduction; path-set limitation = confirmed by algorithm (set semantics cannot represent re-edits — not a "tuning" issue, a structural one).

### C4 — Provider detection matches arguments/paths, not the executable  🔁 ✅  *(severity dispute resolved → MAJOR)*
`adversarial_common/providers.py:13-30`

`detect_provider` joins the whole argv and substring-scans it. Reproduced: `detect_provider(["echo", "/tmp/claude/input.txt"])` → `'claude'`, which then injects `--append-system-prompt-file` (a Claude-only flag) into an unrelated executable and can break it. The Architect rated this **minor**; Inspector and both cross-reviews rated **major** and reproduced it. **Resolved: MAJOR** — misclassification actively corrupts the command, not just mislabels it. Related: `codex` is checked before `claude` (so `~/codex-tools/claude` → codex), and `enhance_cmd_for_project` skips its `-C` flag whenever any token merely *contains* `-C` (`providers.py:78`).

**Fix:** derive identity from the executable/wrapper token (basename + prefix match), not a substring sweep of the joined command line.

### C5 — Shared personas hardcode a specific RF hardware project  📝 *(architecture)*
`personas/architect.md`, `personas/inspector.md`, `personas/cross_review.md`

These files bake in ESP32-S3, CC1101 @ 433 MHz, Goertzel/Kalman, RSSI quantization, BLE spectral scan, WiFi CSI, noise-floor/PLL-settling details. The README declares `personas/` the "single source of truth for all role personas" of a reusable engine imported by both pipelines. Any consumer reviewing non-RF code gets reviewers primed to hallucinate DSP/SNR/antenna findings that cannot exist in the diff; this very review of a Python library was at risk of exactly that priming. Domain/hardware context belongs in per-project injection (`enhance_cmd_for_project` / stdin prefix), not the shared persona library. Single-source (Architect) but **code-confirmed** and unchallenged.

**Risk:** confirmed (the text is literally in the files). The danger is mis-priming and fabricated findings, not a crash.

### C6 — Unbounded subprocess output can exhaust disk and memory  ⚠️
`adversarial_common/runner.py:45-46, 60`

Temp files replace pipes (good — avoids deadlock) but impose no size limit. A noisy or wedged CLI can fill the filesystem across the 600 s default timeout, and the final `out_f.read()` then tries to allocate the entire blob in memory. Needs a byte cap / truncation policy. Added by Cross-Review 1; **not revisited by Cross-Review 2** (single cross-source), but the code confirms no bound exists.

**Risk:** single-source, code-confirmed; realistic DoS for an unattended pipeline.

---

## Minor Findings

| ID | Finding | Location | Status |
|----|---------|----------|--------|
| M1 | **`create_branch` is not idempotent** despite its docstring ("No-op if already exists"). `git branch` exits nonzero for an existing ref → `GitError`. `branch_exists()` already provides the guard. Current caller deletes stale branches first, limiting impact. | `gitops.py:197` | 🔁 reproduced |
| M2 | **`auto_init` does not pin the promised repo-local identity.** `ensure_git_identity` reads `git config user.name` (no `--local`), sees the inherited global identity, and skips writing the local `adversarial-loop` one. `test_auto_init_sets_identity` fails (inherits global `chpo`). | `gitops.py:84, 98` | ✅ test fails |
| M3 | **`stash_dirty` returns a moving position, not an identity.** `stash@{0}` is an index; any intervening stash (hook, concurrent phase) makes `unstash` pop the wrong entry and destroy it. Pin via `git rev-parse stash@{0}` or `stash create`/`store`. | `gitops.py:126` | 📝 |
| M4 | **Mutating git ops run with `timeout=None`.** Only `get_current_branch` bounds its wait (5 s); `checkout`/`merge`/`commit`/`stash` hang forever on a stale `index.lock`, a GPG/credential prompt, or an editor spawn. Module should also set `GIT_TERMINAL_PROMPT=0` and `GIT_EDITOR=true` in the subprocess env. | `gitops.py:23` | 📝 |
| M5 | **`save_artifact` has no path containment** *(downgraded from major — challenge valid).* `Path(out_dir) / name` lets an absolute `name` discard `out_dir` and `../` traverse out. **Resolved:** the only call site passes the constant `final.json` (`write_final_json`); no CLI- or model-controlled filename reaches it, so this is defense-in-depth, not evidenced arbitrary overwrite. Containment check should also reject symlinked parents. | `jsonio.py:44` | 🔁 challenge valid |
| M6 | **`strip_json_wrapper` fence regex mutates JSON string contents + largest-object heuristic.** `re.sub(r'```...')` deletes fences *anywhere*, including inside a JSON string value that quotes a fenced block; `max(candidates, key=len)` can prefer a large example object embedded in prose over the model's actual verdict. | `jsonio.py:22, 35` | 📝 |
| M7 | **`parse_json_output` returns `None` on multiple brace groups.** Strategy "extract `{}`" slices first-`{`-to-last-`}`, so `metadata {"round":1}\n{"verdict":"APPROVE"}` combines into invalid JSON → `None`. The sibling `strip_json_wrapper` already shows the correct `raw_decode`-scan approach. *(Severity dispute: CR1 argued returning `None` on ambiguous input is defensible — agreed; the defect is the silent failure on realistic multi-document output, hence minor, not the safer-than-guessing behavior.)* | `jsonio.py:100` | 🔁 reproduced |
| M8 | **Empty sanitized feature names yield invalid branch refs.** A punctuation-only feature sanitizes to `""`; `create_loop_branch` then builds `loop//1`, which Git rejects (no consecutive slashes). Needs a non-empty fallback or pre-Git validation. | `gitops.py:167, 173` | 📝 |

---

## Nits

| ID | Finding | Location |
|----|---------|----------|
| N1 | **Duplicate `*.py[cod]`** line; and `.venv/` is ignored but **already tracked (1960 files)** — the ignore is inert until `git rm -r --cached .venv`. Confirmed: `git ls-files .venv \| wc -l` → 1960. | `.gitignore:5-6` |
| N2 | **Hard PyYAML dependency with no packaging metadata.** `jsonio` does `import yaml` (hard, no fallback) but the repo ships no `pyproject.toml`/`requirements.txt`; a fresh import fails with a bare `ModuleNotFoundError`. | `jsonio.py:10` |

---

## Hardware / Algorithmic Risk Assessment

**There is no real hardware in this repository** — it is a Python tooling library. The "hardware" dimension therefore splits into two distinct risks:

**1. Literal hardware mis-priming (C5/A1) — confirmed.** This is the only finding involving actual hardware, and it is a *review-quality* risk, not a runtime one. Because the shared personas hardcode ESP32-S3/CC1101/433 MHz, two failure modes are real:
   - **Reviewing non-RF code:** reviewers are primed to invent DSP/SNR/antenna findings that cannot exist (this is how irrelevant findings leak into reviews of unrelated diffs — including this Python library).
   - **Reviewing *different* RF code:** the priming anchors reviewers on wrong specifics (e.g., assuming 433 MHz when the project is 868 MHz, or CC1101 when it's nRF24/SX1278), producing confidently-wrong physics claims.
   Fix is non-code: move domain context to per-project injection.

**2. Algorithmic findings (no physics involved), ranked by confidence:**

| Finding | Risk level | Basis |
|--------|-----------|-------|
| C3 path-set snapshot can't attribute re-edits | **Confirmed by algorithm** | Set-of-paths is structurally insufficient for re-edit detection — not tunable, needs hashes/tree baseline |
| C1-b squash_merge fails on tree-neutral history | **Confirmed by reproduction** | Reproduced with two empty commits → `nothing to commit` |
| C1-a single-commit fast-forward | **Confirmed (test fails)** | `test_squash_merge` asserts `'squashed feat'`, gets `'loop change'` |
| C2 runner non-UTF-8 crash | **Confirmed by reproduction** | Child writing `0xff` → `UnicodeDecodeError` |
| C4 provider spoofing | **Confirmed by reproduction** | `["echo","/tmp/claude/input.txt"]` → `claude` |
| M2 auto_init identity not pinned | **Confirmed (test fails)** | `test_auto_init_sets_identity` fails |
| C6 unbounded output DoS | **Single-source, code-confirmed** | No cap in source; not re-verified by 2nd cross-review |
| C1-c already-merged no-op, C1-d conflict-dirty-index | **Code-confirmed, single-source** | Read from source; not challenged, not reproduced |
| M3 stash-position, M4 no-timeout, M6 fence-mutation, M8 empty-feature | **Theoretical / single-source** | Realistic triggers but not reproduced |

**Bottom line for "will it work on real hardware":** the library has no hardware to run on. What *will* misbehave in real use are the algorithmic boundaries: git history gets corrupted/mis-attributed (C1, C3, M2), the hardened runner crashes on byte noise (C2), and provider commands get silently rewritten (C4). The two failing tests are the loudest signal — they should be treated as merge-blockers regardless of the severity labels above.

---

*Synthesis of 4 review passes → 1 major cluster (C1, 4 sub-defects) + 5 standalone majors/minors disputed down + 8 minors + 2 nits, after dedup and dispute resolution.*
