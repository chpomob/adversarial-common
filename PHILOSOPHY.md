# Philosophy

> The process is the product. Agents are interchangeable workers.

Adversarial pipelines do not depend on a specific agent, a specific model, or a
specific orchestrator. They run **from** any agent and they drive **any other**
agent. The quality of the outcome comes from the process — the confrontation,
the gates, the verification — not from the tool that happens to execute a role
today.

---

## 1. Process over tool

Most agent frameworks bake their methodology into a single harness: install
the plugin, and *that* agent gains the workflow. The methodology dies with the
tool's popularity.

Adversarial-common inverts this. The process lives **outside** the agents:

- it runs from any orchestrator — Hermes Agent, Claude Code, Codex, or a plain
  terminal;
- it exploits any LLM CLI as a worker — Claude Code, Codex, pi, agy, Gemini,
  whatever you have authenticated;
- a new agent is one line in a config file, not a rewrite of the pipeline.

The "best agent" of the year is obsolete in six months. The process is not.
Tools churn; the assembly line stays.

## 2. Agents are workers, roles are the contract

A pipeline defines roles — `DEV`, `REVIEW`, `ARBITER` — and each role is filled
by an independent agent. Independence is the point: two models that share the
same family share the same blind spots. Pairing different model families by
design (builder on Codex, critic on Claude, arbiter on GLM) breaks the echo
chamber that single-model workflows cannot escape.

Roles are resolved per-run through `providers.py` and `--provider-config`.
Nothing in the pipeline hardcodes a model, a vendor, or a CLI. Swap a worker
without stopping the line.

## 3. The floor and the ceiling

Process-first is not tool-indifferent. Be precise about what each layer
guarantees:

- **The process guarantees the floor.** Confrontation between independent
  roles, code-enforced gates (zero-skip, watchdog), git-native isolation,
  deterministic resume after crashes, evidence-based verification — none of
  this depends on which model fills a role. You never fall below the floor.
- **The tool sets the ceiling.** A stronger reviewer finds more, a faster
  builder iterates more. Plug the best worker you can afford into each role.

Sell the floor. Hire for the ceiling.

## 4. Code-enforced, not prompt-enforced

Skills that rely on persuasion ("you MUST verify before claiming") are soft:
an agent can rationalize its way past any instruction. Adversarial-common
moves the invariants into code:

- subprocess execution with process-group kill on timeout and non-UTF-8
  tolerance (`runner.py`);
- JSON extraction that survives markdown fences, prose, and malformed output
  (`jsonio.py`);
- dirty-tree baselines that detect sandbox escape (`snapshot.py`);
- gate enforcement, watchdog, and `state.json` resume that do not ask the
  model for permission.

Prompt-level tables and red flags are useful seasoning. The gates are the meal.

## 5. Git is the source of truth

Reviews inspect the real `git diff`, not generated text. Every phase is a
commit; every loop is a branch; rollback is native; a crash leaves a recoverable
state. When a pipeline disagrees with a model's self-report, git wins.

## 6. Adversarial-common is the foundation — build on it

This package is the master brick. Any new adversarial skill is built from the
same primitives:

| Module | What it gives you |
|---|---|
| `runner.py` | hardened subprocess execution (killpg, temp-file IO, UTF-8 tolerance) |
| `providers.py` | CLI detection, persona injection, role-based command resolution |
| `jsonio.py` | 3-strategy JSON extraction + artifact persistence |
| `gitops.py` | init, branch, commit, diff, stash, squash-merge, reject-marker |
| `snapshot.py` | dirty-tree baseline for sandbox write detection |
| `personas/` | 15+ adversarial personas (builder, critic, fixer, verifier, judge, …) |

To build a new adversarial skill: define its roles, its gate, its artifacts —
then compose the primitives. No new infrastructure, no lock-in, 0BSD.

If adversarial-common were a factory, the assembly line would be the product,
the workers would be the models, and anyone could open a new workshop with the
same machinery. That is the intent.
