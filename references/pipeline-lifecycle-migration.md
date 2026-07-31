# Shared pipeline lifecycle migration design

## Scope and sequencing

This document is the P22 design gate for extracting the lifecycle code shared
by `adversarial-spec`, `adversarial-plan`, `adversarial-code-loop`, and
`adversarial-code-review`. P22 adds and tests the common API, but does not edit
any consumer. P23-P26 migrate one orchestrator at a time after all five
repository suites are green against the unmodified consumers.

The inventory below was taken from the four current orchestrators. Line numbers
identify the definitions in the pre-migration files; call sites are summarized
where their shape matters to the contract.

## Consumer inventory

| Contract | adversarial-spec | adversarial-plan | adversarial-code-loop v4 | adversarial-code-review | Canonical semantics and intentional differences |
|---|---|---|---|---|---|
| `banner(title, *, ci=False, stream=None)` | `_banner(title)` at `scripts/adversarial_spec.py:110`; called for branch/write/challenge/revise/verify | `_banner(title, ci=False)` at `scripts/adversarial_plan.py:92`; same phases, with CI suppression | `_banner(title)` at `scripts/adversarial_loop_v4.py:118`; called for resume/branch/build/review/fix/verify/arbiter | no helper | Print the existing 60-column banner. CI suppression is policy, default false. An injectable stream makes output testable. |
| `write_json(out_dir, name, payload)` | `_write_json(out_dir, name, payload)` at `adversarial_spec.py:114` | same signature at `adversarial_plan.py:99` | same signature at `adversarial_loop_v4.py:92`; also persists `state.json` | final JSON is written through `_write_final(args, verdict, **extra)` at `adversarial_review.py:125` | Pretty JSON, trailing newline, create parents, and atomically replace the destination. Review keeps its richer final writer; it may use this only for ordinary artifacts. |
| `ensure_finding_ids(findings)` | `_ensure_ids(findings)` at `adversarial_spec.py:119`; `_finalize_finding_ids` additionally rekeys warnings | `_ensure_ids(findings)` at `adversarial_plan.py:104` | `_ensure_ids(findings)` at `adversarial_loop_v4.py:122` | findings use a review-specific schema validator instead | Mutate and return the list. Blank IDs become `finding-N`; duplicates gain `-N` repeatedly until unique. Non-mapping findings are rejected rather than failing later. Warning rekeying remains spec policy and is not folded into this primitive. |
| `SETTLED_STATUSES`, `is_settled_status(status)`, `unresolved_findings(findings, results, *, settled_statuses=...)` | `_SETTLED_STATUSES` at `adversarial_spec.py:97`; `_unresolved(findings, results)` at `:160` | status set at `adversarial_plan.py:83`; helper at `:116` | status set at `adversarial_loop_v4.py:72`; helper at `:618` | no verify loop | Canonical settled values are exactly `resolved` and `rejected`. A result without an ID never settles anything. A configurable status collection is the explicit policy boundary. |
| `threshold_overrides(args, threshold_env, *, environ=None, error_type=ValueError)` | `_threshold_overrides(args)` at `adversarial_spec.py:169`; brief-specific then shared env | no local helper; current plan preflight is implemented in `main`/runner paths without the same extraction | `_threshold_overrides(args)` at `adversarial_loop_v4.py:138`; loop aliases then shared env | `_threshold_overrides(args)` at `adversarial_review.py:1163`; includes `min_source_lines`, raises `ReviewError` | Precedence is CLI attribute, then the ordered env names. Values must be non-negative integers. The env map and exception factory/type are consumer policy; an injected env mapping prevents tests from touching process state. |
| `PreflightPolicy`, `PreflightResult`, `preflight(args, text, out_dir, policy=...)` | `_preflight(args, brief_text, out_dir)` at `adversarial_spec.py:579`; returns a five-tuple and writes an option-aware final | preflight is embedded in plan orchestration and runner glue; context kind is `spec` | `_preflight(args, spec_text, out_dir)` at `adversarial_loop_v4.py:187`; returns `(text, ok)` and writes loop fields | `_preflight_source(source, args)` at `adversarial_review.py:1202`; no input-cap step and returns context | Canonical result contains effective text, context, complexity, cap events, and `ok`. Policy supplies context kind (or resolver), env map, whether to enforce the input cap, blocked-artifact writer, execution metadata, blocked extras, stderr reporter, and whether result metadata is attached to `args`. This preserves consumer artifact schemas without imports. If capped and truncation is disabled, otherwise-valid context is blocked as `input_exceeds_max_chars`. |
| `record_phase(state, label, result, ledger, *, include_epistemic=False)` | `_record_phase(...)` at `adversarial_spec.py:639` | only `_record_provider_history(state, result)` at `adversarial_plan.py:125`; later migration opts into the full record | `_record_phase(...)` at `adversarial_loop_v4.py:286`; also copies epistemic distribution | review records calls through `_record_call(args, label, role, phase, result)` at `adversarial_review.py:545` | Append bounded attempts/cap events, update the ledger summary, copy mapping provider decisions, and deduplicate warnings. Loop's epistemic copy is an explicit flag; review retains role-specific call records. Malformed result subfields are treated as empty. |
| `GitSetupPolicy`, `setup_git(workdir, feature, state=None, *, policy=...)` | `_setup_git(...)` at `adversarial_spec.py:738`; prefix `spec`, ignore `.adversarial-spec/` | `_setup_git(...)` at `adversarial_plan.py:350`; prefix `plan`, ignore `.adversarial-plan/` | calls `scripts.phases.phase_git.setup_git(workdir, feature, parent_branch)` at `adversarial_loop_v4.py:778` | review uses isolated worktrees, not this lifecycle | Default adapter uses common `gitops`; policy supplies prefix, ignore entry, optional parent branch, and an optional setup callback for loop. It initializes a non-repo, ensures identity in a repo, establishes `stash_id` before mutation, stashes dirty files, creates/checks out the branch, records branch point, and returns an error mapping instead of raising. No phase import is allowed. |
| `RestoreResult`, `restore_git(workdir, state, out_dir=None, *, policy=...)` | `_restore(workdir, state)` at `adversarial_spec.py:717`; currently fails to persist cleared stash | `_restore(workdir, state)` at `adversarial_plan.py:172`; same persistence drift | `_restore(workdir, state, out_dir)` at `adversarial_loop_v4.py:639`; clears and writes state; additionally removes concurrent worktrees and warns with manual recovery command | `run_diff_git` has its own isolated-worktree cleanup around `adversarial_review.py:1350` | Always return to the parent before unstashing; never unstash after checkout failure. After a successful pop, clear `state['stash_id']` and atomically persist `state.json` when `out_dir` is supplied. Policy supplies pre-restore cleanup and reporting; loop passes orphan-worktree cleanup. Return structured status so callers/tests can distinguish checkout, stash, and persistence failures. |
| `RetrospectivePolicy`, `log_retrospective(...)`, `phase_failure(...)` | `_log_retrospective(...)` at `adversarial_spec.py:687`; `_phase_failed(...)` at `:706` | same helpers at `adversarial_plan.py:142` and `:161` | `_phase_failed(...)` at `adversarial_loop_v4.py:631` writes state but has no retrospective | review records phase failures in final JSON through `_write_phase_error` at `adversarial_review.py:943` | Uniform git-pipeline behavior appends the same bounded UTC Markdown record under the run artifact directory. `phase_failure` reports, best-effort logs, optionally mutates/persists resumable state, and returns the configured infrastructure code. Clock, filename, stdout bound, reporter, and state recorder are policy. Review retains its structured failure artifact rather than receiving a second log. |
| `FinishPolicy`, `finish_pipeline(...)` | `_finish(...)` at `adversarial_spec.py:777`; common git squash/reject, rich spec final payload | `_finish(...)` at `adversarial_plan.py:403`; common git squash/reject, HTML and CI options | `_finish(...)` at `adversarial_loop_v4.py:689`; delegates finalize to `phase_git`, supports ARBITRATED/conditions/resume state | final writing and CI are `_write_final`/`_ci_exit`, not git finalization | Shared orchestration writes human and machine artifacts, invokes an injected/default git finalizer, maps legacy/CI exits, and runs optional post-write callbacks. Pipeline label, loop label, approved verdicts, messages, exit map, finalizer, payload builder, final writer, report renderer, state completion, and reporter are policy. This is the principal callback boundary: the base never imports consumer phase/provider modules. |
| `ci_exit_from_final(out_dir, legacy_code, *, fail_on_selector=None, final_name='final.json', error_writer=None)` | `_ci_exit_code_from_final(out_dir, legacy_code)` at `adversarial_spec.py:287`; final metadata preferred | plan calls imported `ci_exit_code` directly at `adversarial_plan.py:459` | no CI flag; legacy verdict map `_EXIT_BY_VERDICT` at `adversarial_loop_v4.py:79` | `_ci_exit(args, legacy_code)` at `adversarial_review.py:167`; final metadata preferred, rebuilds setup error if unreadable | If CI is disabled, callers do not invoke it. When invoked, read the final artifact, prefer an integer non-boolean recorded `ci.exit_code`, otherwise delegate to common runner policy. Missing/malformed artifacts map to infrastructure and optionally call an error writer. Findings may live under `findings` or `finding_details`. |
| `positive_int(value)`, `non_negative_int(value)` | `_positive_int` at `adversarial_spec.py:962`; `_non_negative_int` at `:973` | `_positive_int` at `adversarial_plan.py:845`; flags currently requiring zero need deliberate migration | same names at `adversarial_loop_v4.py:1163` and `:1175` | `_positive_arg` at `adversarial_review.py:1446`; `_non_negative_arg` at `:1436` | Use `argparse.ArgumentTypeError`; reject booleans; accept integer strings. Messages use the more diagnostic spec/loop wording. Positive means `> 0`, non-negative means `>= 0`. |

## Public API and dependency boundary

`adversarial_common.pipeline_base` will export the following reviewed surface,
also re-exported from `adversarial_common`:

- constants and value types: `SETTLED_STATUSES`, `PreflightPolicy`,
  `PreflightResult`, `GitSetupPolicy`, `RestorePolicy`, `RestoreResult`,
  `RetrospectivePolicy`, and `FinishPolicy`;
- output/findings: `banner`, `write_json`, `ensure_finding_ids`,
  `is_settled_status`, and `unresolved_findings`;
- input/phase lifecycle: `threshold_overrides`, `preflight`, and `record_phase`;
- git/final lifecycle: `setup_git`, `restore_git`, `log_retrospective`,
  `phase_failure`, `finish_pipeline`, and `ci_exit_from_final`;
- argparse types: `positive_int` and `non_negative_int`.

The module may depend only on the Python standard library and the common
`costs`, `gates`, `gitops`, `jsonio`, and `runner` modules. It must not import an
orchestrator, `scripts.phases`, personas, provider registries, or provider
implementations. Every dependency that carries consumer policy is a frozen
configuration value or callback. Callbacks receive plain values or a context
mapping; they do not receive a consumer module.

Default policies target the spec/plan shape. The loop supplies its
`phase_git.setup_git`/`finalize_git`, resumable-state completion, epistemic, and
worktree-cleanup policies at migration time. Review supplies its source-kind
resolver, `ReviewError`, blocked/final writers, and CI setup-error callback;
review does not adopt the branch lifecycle.

## Migration order and regression matrix

P22 makes no consumer edits. The later migrations should remain one at a time:
spec (P23), plan (P24), loop v4 plus its compatibility alias (P25), then review
(P26). After each migration, run the common suite and all four consumer suites.

| ID | Contract / divergence locked | Unit or contract regression | Consumer suites required |
|---|---|---|---|
| PB-01 | Banner shape and CI suppression | capture normal, CI, and injected-stream output | spec, plan, loop |
| PB-02 | Pretty JSON with newline and atomic replacement | nested path, serialization failure leaves old file, `os.replace` path | all five repositories |
| PB-03 | Finding IDs remain unique under blank and repeated IDs | blank, whitespace, duplicate chains, invalid finding | spec, plan, loop |
| PB-04 | Only resolved/rejected IDs settle findings | missing ID, unknown status, configurable status set | spec, plan, loop |
| PB-05 | Threshold precedence and typed errors | CLI > first env > later env; malformed/negative env; review `ReviewError` | spec, loop, review |
| PB-06 | Cap/truncate/context/complexity preflight semantics | pass, truncate, cap-block, context-block; callable kind; attached metadata | spec, loop, review and plan preflight tests |
| PB-07 | Blocked artifact remains consumer-defined and precedes providers/git | callback payload, callback failure, no git/provider imports | all five repositories |
| PB-08 | Phase evidence aggregation is bounded and defensive | success/failure, malformed execution, attempts/events/history/warning dedupe | spec, loop; plan/review unaffected |
| PB-09 | Loop epistemic distribution is opt-in | labels/distribution aliases and disabled case | loop |
| PB-10 | Git setup preserves recovery state and prefixes | existing repo, auto-init, stash, branch/ignore, callback, exception mapping | spec, plan, loop |
| PB-11 | Restore never unstashes on the wrong branch | already-parent, checkout success/failure, manual-recovery report | spec, plan, loop |
| PB-12 | Successful stash pop atomically clears persisted state | inspect `state.json` after pop; replace failure is reported without restoring stale in-memory stash ID | spec, plan, loop |
| PB-13 | Loop orphan worktree cleanup stays policy-owned | cleanup callback success/failure does not mask restore | loop |
| PB-14 | Retrospective is uniform across git pipelines | exact fields, UTC date, 200-character stdout tail, custom clock/file | spec, plan, loop |
| PB-15 | Failure logging remains best-effort and resumable | log/write/reporter callback errors; infra exit preserved | spec, plan, loop |
| PB-16 | Default and callback git finalizers preserve verdict semantics | approved merge/no-merge, reject marker, ARBITRATED callback, finalize error | spec, plan, loop |
| PB-17 | Final payload and optional report/state callbacks stay policy-owned | custom payload/writer/post-write/state-complete success and error | spec, plan, loop |
| PB-18 | Legacy exits and CI exits remain stable | every verdict, infrastructure precedence, fail-on selector | all five repositories |
| PB-19 | Recorded CI exit is authoritative and bool is not an int | recorded int/bool, fallback computation, malformed/missing final | spec, plan, review |
| PB-20 | Validator domains and diagnostics are stable | integers/strings, zero, negatives, text, booleans | all five repositories |
| PB-21 | Common package exports exactly usable symbols | import module and package-level aliases; `__all__` contract | all five repositories |
| PB-22 | Dependency direction remains acyclic | AST import audit forbids provider, phase, and consumer imports | common |
| PB-23 | No consumer is migrated during P22 | AST/import scan of all four orchestrators finds no `pipeline_base` import | all five repositories |

Git lifecycle unit tests use an injected fake adapter or a fresh temporary
repository. They never point at a developer checkout. The final P22 gate is:
common tests green, then the unmodified spec, plan, code-loop, and code-review
suites green, for five repository suites total.
