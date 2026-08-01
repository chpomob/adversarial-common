"""Shared lifecycle primitives for adversarial pipeline orchestrators.

The module deliberately depends only on the common runtime.  Consumer-specific
phase and provider modules belong behind the policy callbacks defined here; an
orchestrator must never be imported back into this base.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from collections.abc import Callable, Collection, Mapping, MutableMapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TextIO

from . import gates, gitops, runner
from .costs import CostLedger


SETTLED_STATUSES = frozenset({"resolved", "rejected"})


def _stdout_report(message: str) -> None:
    print(message)


def _stderr_report(message: str) -> None:
    print(message, file=sys.stderr)


def _safe_report(reporter: Callable[[str], None], message: str) -> None:
    """Report without allowing diagnostic output to mask lifecycle cleanup."""
    try:
        reporter(message)
    except Exception:
        pass


def _atomic_write_text(path: Path, content: str) -> Path:
    """Atomically replace *path* with *content* and clean up failed temp files."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=path.parent,
            prefix=f".{path.name}.", suffix=".tmp", delete=False,
        ) as handle:
            temporary = Path(handle.name)
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except Exception:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
        raise
    return path


def banner(title: Any, *, ci: bool = False, stream: TextIO | None = None) -> None:
    """Print the suite's standard phase banner unless CI output is enabled."""
    if ci:
        return
    target = sys.stdout if stream is None else stream
    print(f"\n{'=' * 60}\n  {title}\n{'=' * 60}", file=target)


def write_json(out_dir: str | os.PathLike[str], name: str, payload: Any) -> Path:
    """Atomically persist pretty-printed JSON under *out_dir*."""
    content = json.dumps(payload, indent=2) + "\n"
    return _atomic_write_text(Path(out_dir) / name, content)


def ensure_finding_ids(findings: list[MutableMapping[str, Any]]) -> list:
    """Assign unique, non-empty string IDs to findings in place."""
    seen: set[str] = set()
    for index, finding in enumerate(findings, 1):
        if not isinstance(finding, MutableMapping):
            raise TypeError(f"finding {index} must be a mutable mapping")
        finding_id = str(finding.get("id") or "").strip() or f"finding-{index}"
        while finding_id in seen:
            finding_id = f"{finding_id}-{index}"
        finding["id"] = finding_id
        seen.add(finding_id)
    return findings


def is_settled_status(
    status: Any, *, settled_statuses: Collection[str] = SETTLED_STATUSES,
) -> bool:
    """Return whether *status* is non-blocking under the selected policy."""
    return status in settled_statuses


def unresolved_findings(
    findings: Sequence[Mapping[str, Any]],
    results: Sequence[Mapping[str, Any]],
    *,
    settled_statuses: Collection[str] = SETTLED_STATUSES,
) -> list[Mapping[str, Any]]:
    """Return findings without a matching result in a settled status."""
    settled = {
        result.get("id")
        for result in results
        if isinstance(result, Mapping)
        and result.get("id") is not None
        and is_settled_status(
            result.get("status"), settled_statuses=settled_statuses,
        )
    }
    return [
        finding for finding in findings
        if not isinstance(finding, Mapping) or finding.get("id") not in settled
    ]


def _policy_error(error_type: Callable[[str], Exception], message: str) -> Exception:
    error = error_type(message)
    if not isinstance(error, Exception):
        raise TypeError("error_type must return an exception")
    return error


def threshold_overrides(
    args: Any,
    threshold_env: Mapping[str, Sequence[str]],
    *,
    environ: Mapping[str, str] | None = None,
    error_type: Callable[[str], Exception] = ValueError,
) -> dict[str, int]:
    """Resolve thresholds using CLI attribute then ordered environment names."""
    environment = os.environ if environ is None else environ
    overrides: dict[str, int] = {}
    for name, env_names in threshold_env.items():
        value = getattr(args, name, None)
        source = name
        if value is None:
            for env_name in env_names:
                if env_name in environment:
                    value = environment[env_name]
                    source = f"${env_name}"
                    break
        if value is None:
            continue
        try:
            if isinstance(value, bool):
                raise ValueError
            parsed = int(value)
        except (TypeError, ValueError) as exc:
            raise _policy_error(
                error_type, f"{source} must be a non-negative integer",
            ) from exc
        if parsed < 0:
            raise _policy_error(
                error_type, f"{source} must be a non-negative integer",
            )
        overrides[name] = parsed
    return overrides


def _default_execution_settings(args: Any) -> dict[str, Any]:
    return {
        "max_retries": getattr(args, "max_retries", 3),
        "max_input_chars": getattr(
            args, "max_input_chars", runner.DEFAULT_MAX_INPUT_CHARS,
        ),
        "max_output_chars": getattr(
            args, "max_output_chars", runner.DEFAULT_MAX_OUTPUT_CHARS,
        ),
        "truncate_input": bool(getattr(args, "truncate_input", False)),
        "show_costs": bool(getattr(args, "show_costs", False)),
        "max_agents": getattr(args, "max_agents", 6),
    }


def _default_blocked_writer(
    out_dir: str | os.PathLike[str], verdict: str, payload: Mapping[str, Any],
) -> Path:
    return write_json(out_dir, "final.json", {"verdict": verdict, **payload})


@dataclass(frozen=True)
class PreflightPolicy:
    """Consumer policy for :func:`preflight`."""

    context_kind: str | Callable[[Any, str], str] = "input"
    threshold_env: Mapping[str, Sequence[str]] = field(default_factory=dict)
    environ: Mapping[str, str] | None = None
    threshold_error_type: Callable[[str], Exception] = ValueError
    enforce_input_cap: bool = True
    check_context: Callable[..., Mapping[str, Any]] = gates.check_context
    estimate_complexity: Callable[..., Mapping[str, Any]] = gates.estimate_complexity
    enforce_cap: Callable[[str, int], tuple[str, bool]] = gates.enforce_input_cap
    execution_settings: Callable[[Any], Mapping[str, Any]] = _default_execution_settings
    blocked_writer: Callable[
        [str | os.PathLike[str], str, Mapping[str, Any]], Any
    ] = _default_blocked_writer
    blocked_extra: Mapping[str, Any] | Callable[
        [Any, "PreflightResult"], Mapping[str, Any]
    ] = field(default_factory=dict)
    reporter: Callable[[str], None] = _stderr_report
    attach_to_args: bool = True


@dataclass(frozen=True)
class PreflightResult:
    """Normalized result of a primary-input preflight."""

    effective_text: str
    context: Mapping[str, Any]
    complexity: Mapping[str, Any]
    cap_events: list[dict[str, Any]]
    ok: bool


def preflight(
    args: Any,
    text: str,
    out_dir: str | os.PathLike[str],
    *,
    policy: PreflightPolicy | None = None,
) -> PreflightResult:
    """Apply input cap, context gate, and complexity estimation before mutation."""
    selected = policy or PreflightPolicy()
    cap_limit = getattr(args, "max_input_chars", None)
    if cap_limit is None:
        cap_limit = runner.DEFAULT_MAX_INPUT_CHARS
    truncate = bool(getattr(args, "truncate_input", False))
    capped = text
    truncated = False
    cap_events: list[dict[str, Any]] = []
    if selected.enforce_input_cap:
        capped, truncated = selected.enforce_cap(text, cap_limit)
        if truncated:
            cap_events.append({
                "kind": "input",
                "phase": "preflight",
                "limit": cap_limit,
                "original_chars": len(text),
                "truncated": truncate,
            })
    effective_text = capped if truncate else text
    context_kind = selected.context_kind
    if callable(context_kind):
        context_kind = context_kind(args, effective_text)
    overrides = threshold_overrides(
        args, selected.threshold_env, environ=selected.environ,
        error_type=selected.threshold_error_type,
    )
    context = dict(selected.check_context(context_kind, effective_text, overrides))
    if truncated and not truncate and context.get("ok"):
        context.update({
            "ok": False,
            "reason": "input_exceeds_max_chars",
            "max_input_chars": cap_limit,
            "input_chars": len(text),
        })
    complexity = dict(selected.estimate_complexity(
        effective_text, max_agents=getattr(args, "max_agents", 6),
    ))
    result = PreflightResult(
        effective_text=effective_text,
        context=context,
        complexity=complexity,
        cap_events=cap_events,
        ok=bool(context.get("ok")),
    )
    if selected.attach_to_args:
        args._context = context
        args._complexity = complexity
        args._preflight_cap_events = cap_events
    if result.ok:
        return result

    payload: dict[str, Any] = {
        "status": "blocked",
        "context_blocked": True,
        "reason": context.get("reason", "context_blocked"),
        "context": context,
        "thresholds": context.get("thresholds", {}),
        "complexity": complexity,
        "execution": dict(selected.execution_settings(args)),
        "attempts": [],
        "cap_events": cap_events,
        "calls": [],
        "costs": CostLedger().summary(),
        "warnings": [],
    }
    extra = selected.blocked_extra
    if callable(extra):
        extra = extra(args, result)
    payload.update(dict(extra))
    selected.blocked_writer(out_dir, "CONTEXT_BLOCKED", payload)
    _safe_report(
        selected.reporter,
        f"X context blocked: {context.get('reason', 'context_blocked')}",
    )
    return result


def _mapping_list(value: Any) -> list[Mapping[str, Any]]:
    if not isinstance(value, (list, tuple)):
        return []
    return [item for item in value if isinstance(item, Mapping)]


def record_phase(
    state: MutableMapping[str, Any],
    label: str,
    result: Any,
    ledger: Any,
    *,
    include_epistemic: bool = False,
) -> dict[str, Any]:
    """Attach bounded runner evidence and the current ledger to run state."""
    result_map = result if isinstance(result, Mapping) else {}
    runtime = result_map.get("execution", {})
    if not isinstance(runtime, Mapping):
        runtime = {}
    attempts = _mapping_list(runtime.get("attempts"))
    cap_events = _mapping_list(runtime.get("cap_events"))
    call = {
        "label": label,
        "ok": result_map.get("exit_code") == 0,
        "attempts": [dict(item) for item in attempts],
        "cap_events": [dict(item) for item in cap_events],
    }
    state.setdefault("calls", []).append(call)
    state.setdefault("attempts", []).extend(
        {"phase": label, **dict(item)} for item in attempts
    )
    state.setdefault("cap_events", []).extend(
        {"phase": label, **dict(item)} for item in cap_events
    )
    state["costs"] = ledger.summary()
    for decision in _mapping_list(result_map.get("provider_history")):
        state.setdefault("provider_history", []).append(dict(decision))
    warnings = result_map.get("warnings", [])
    if isinstance(warnings, (list, tuple)):
        for warning in warnings:
            if warning not in state.setdefault("warnings", []):
                state["warnings"].append(warning)
    if include_epistemic:
        distribution = result_map.get("epistemic_distribution")
        if not isinstance(distribution, Mapping):
            distribution = result_map.get("epistemic_labels")
        if isinstance(distribution, Mapping):
            state["epistemic_labels"] = dict(distribution)
    return call


@dataclass(frozen=True)
class GitSetupPolicy:
    """Git namespace and adapter policy for :func:`setup_git`."""

    prefix: str = "loop"
    gitignore_entry: str | None = ".adversarial-loop/"
    parent_branch: str | None = None
    git_adapter: Any = gitops
    setup_callback: Callable[[Mapping[str, Any]], Mapping[str, Any]] | None = None


def setup_git(
    workdir: str | os.PathLike[str],
    feature: str,
    state: MutableMapping[str, Any] | None = None,
    *,
    policy: GitSetupPolicy | None = None,
) -> dict[str, Any]:
    """Prepare a pipeline branch while keeping stash recovery state current."""
    selected = policy or GitSetupPolicy()
    target_state = state if state is not None else {}
    target_state.setdefault("stash_id", "")
    context = {
        "workdir": workdir,
        "feature": feature,
        "state": target_state,
        "policy": selected,
    }
    try:
        if selected.setup_callback is not None:
            result = dict(selected.setup_callback(context))
            if result.get("exit_code", 0) == 0:
                for name in (
                    "parent_branch", "branch", "branch_point", "stash_id",
                ):
                    if name in result:
                        target_state[name] = result[name]
            return result

        adapter = selected.git_adapter
        if adapter.detect_enclosing_repo(workdir):
            adapter.ensure_git_identity(workdir)
            detected_parent = adapter.get_current_branch(workdir)
        else:
            adapter.auto_init(workdir)
            detected_parent = "main"
        parent = selected.parent_branch or detected_parent
        target_state["parent_branch"] = parent

        def _record_stash_id(stash_id):
            # Fires the instant `git stash push -u` succeeds (before SHA
            # resolution), so a KeyboardInterrupt/BaseException mid-resolution
            # still leaves state holding a recoverable id for restore_git.
            target_state["stash_id"] = stash_id

        target_state["stash_id"] = adapter.stash_dirty(
            workdir, on_pushed=_record_stash_id,
        )
        branch = adapter.create_loop_branch(
            workdir, feature, parent, prefix=selected.prefix,
        )
        target_state["branch"] = branch
        adapter.checkout(workdir, branch)
        branch_point = adapter.record_branch_point(workdir, parent)
        target_state["branch_point"] = branch_point
        if selected.gitignore_entry:
            adapter.ensure_gitignore(workdir, selected.gitignore_entry)
        return {
            "exit_code": 0,
            "parent_branch": parent,
            "branch": branch,
            "branch_point": branch_point,
            "stash_id": target_state["stash_id"],
        }
    except Exception as exc:
        return {"exit_code": 1, "error": str(exc)}


@dataclass(frozen=True)
class RestorePolicy:
    """Cleanup, persistence, and reporting policy for :func:`restore_git`."""

    git_adapter: Any = gitops
    pre_restore: Callable[[Mapping[str, Any]], None] | None = None
    state_writer: Callable[[str | os.PathLike[str], str, Any], Path] = write_json
    state_filename: str = "state.json"
    reporter: Callable[[str], None] = _stdout_report


@dataclass(frozen=True)
class RestoreResult:
    """Structured outcome from a best-effort git restoration."""

    branch_restored: bool
    stash_restored: bool
    state_persisted: bool
    error: str = ""
    cleanup_error: str = ""

    @property
    def ok(self) -> bool:
        return not self.error


def restore_git(
    workdir: str | os.PathLike[str],
    state: MutableMapping[str, Any],
    out_dir: str | os.PathLike[str] | None = None,
    *,
    policy: RestorePolicy | None = None,
) -> RestoreResult:
    """Return to the parent branch, restore a stash, and persist cleared state."""
    selected = policy or RestorePolicy()
    cleanup_error = ""
    context = {"workdir": workdir, "state": state, "out_dir": out_dir}
    if selected.pre_restore is not None:
        try:
            selected.pre_restore(context)
        except Exception as exc:
            cleanup_error = str(exc)
            _safe_report(selected.reporter, f"! pre-restore cleanup failed: {exc}")

    parent = str(state.get("parent_branch", "") or "")
    try:
        if parent and selected.git_adapter.get_current_branch(workdir) != parent:
            selected.git_adapter.checkout(workdir, parent)
    except Exception as exc:
        message = f"could not restore branch {parent!r}: {exc}"
        _safe_report(selected.reporter, f"! {message}")
        stash_id = str(state.get("stash_id", "") or "")
        if stash_id:
            _safe_report(
                selected.reporter,
                "! stashed changes NOT restored — recover manually with: "
                f"git stash pop {stash_id}",
            )
        return RestoreResult(False, False, False, message, cleanup_error)

    stash_id = str(state.get("stash_id", "") or "")
    if not stash_id:
        return RestoreResult(True, True, False, cleanup_error=cleanup_error)
    try:
        selected.git_adapter.unstash(workdir, stash_id)
    except Exception as exc:
        message = f"could not pop {stash_id}: {exc}"
        _safe_report(selected.reporter, f"! {message}")
        return RestoreResult(True, False, False, message, cleanup_error)

    state["stash_id"] = ""
    if out_dir is None:
        return RestoreResult(True, True, False, cleanup_error=cleanup_error)
    try:
        selected.state_writer(out_dir, selected.state_filename, state)
    except Exception as exc:
        message = f"could not persist restored state: {exc}"
        _safe_report(selected.reporter, f"! {message}")
        return RestoreResult(True, True, False, message, cleanup_error)
    return RestoreResult(True, True, True, cleanup_error=cleanup_error)


@dataclass(frozen=True)
class RetrospectivePolicy:
    """Formatting and failure-state policy for retrospective records."""

    filename: str = "ISSUES.md"
    stdout_chars: int = 200
    clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc)
    reporter: Callable[[str], None] = _stdout_report
    infrastructure_exit: int = 1
    record_state_failure: bool = False
    state_recorder: Callable[[MutableMapping[str, Any], Any, str], None] | None = None


def log_retrospective(
    label: str,
    result: Any,
    feature: str,
    branch: str,
    out_dir: str | os.PathLike[str],
    *,
    policy: RetrospectivePolicy | None = None,
) -> Path:
    """Append one bounded, UTC pipeline failure record to the artifact log."""
    selected = policy or RetrospectivePolicy()
    result_map = result if isinstance(result, Mapping) else {}
    now = selected.clock()
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    day = now.astimezone(timezone.utc).strftime("%Y-%m-%d")
    stdout = str(result_map.get("stdout", "") or "")[-selected.stdout_chars:]
    entry = (
        f"\n### {day} — {label} failed for {feature}\n\n"
        f"- **Phase:** {label}\n"
        f"- **Branch:** {branch}\n"
        f"- **Error:** {result_map.get('error', 'unknown error')}\n"
        f"- **Stdout (last {selected.stdout_chars} chars):** {stdout!r}\n"
        "- **Auto-logged by pipeline**\n"
    )
    path = Path(out_dir) / selected.filename
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(entry)
    return path


def phase_failure(
    label: str,
    result: Any,
    state: MutableMapping[str, Any],
    out_dir: str | os.PathLike[str],
    *,
    policy: RetrospectivePolicy | None = None,
) -> int:
    """Report and retrospectively log a phase failure without masking it."""
    selected = policy or RetrospectivePolicy()
    result_map = result if isinstance(result, Mapping) else {}
    error = result_map.get("error", "unknown error")
    _safe_report(selected.reporter, f"X {label} failed: {error}")
    if selected.record_state_failure:
        state["error"] = f"{label}: {error}"
        if selected.state_recorder is not None:
            try:
                selected.state_recorder(state, out_dir, f"{label}_failed")
            except Exception as exc:
                _safe_report(
                    selected.reporter,
                    f"! could not persist failure state: {exc}",
                )
    try:
        log_retrospective(
            label, result_map, str(state.get("feature", "unknown")),
            str(state.get("branch", "")), out_dir, policy=selected,
        )
    except Exception as exc:
        _safe_report(
            selected.reporter, f"! could not write retrospective log: {exc}",
        )
    return selected.infrastructure_exit


def _default_final_md(context: Mapping[str, Any]) -> str:
    conditions = list(context.get("conditions", []))
    policy = context["policy"]
    lines = [
        f"# {policy.pipeline_name} — {context['feature']}",
        "",
        f"- Verdict: {context['verdict']}",
        f"- {policy.loop_label}: {context['loops']}",
        f"- Finished: {datetime.now(timezone.utc).isoformat()}",
    ]
    if context.get("reason"):
        lines.append(f"- Reason: {context['reason']}")
    if conditions:
        lines.append("- Conditions:")
        lines.extend(f"  - {condition}" for condition in conditions)
    return "\n".join(lines) + "\n"


def _default_finalize(context: Mapping[str, Any]) -> Mapping[str, Any]:
    policy = context["policy"]
    args = context["args"]
    state = context["state"]
    verdict = context["verdict"]
    if verdict in policy.approved_verdicts:
        if not bool(getattr(args, "no_merge", False)):
            policy.git_adapter.squash_merge(
                context["workdir"], state["branch"], state["parent_branch"],
                f"squash: {context['feature']} — {policy.approval_label} approved",
            )
            return {"exit_code": 0, "merged": True}
        return {"exit_code": 0, "merged": False}
    policy.git_adapter.reject_marker(
        context["workdir"],
        f"{context['feature']} — {policy.approval_label} {verdict}",
    )
    return {"exit_code": 0, "merged": False}


@dataclass(frozen=True)
class FinishPolicy:
    """Final artifacts, git finalization, and exit policy."""

    pipeline_name: str = "Adversarial Pipeline"
    approval_label: str = "pipeline"
    loop_label: str = "Loops"
    approved_verdicts: frozenset[str] = frozenset({"APPROVED"})
    exit_by_verdict: Mapping[str, int] = field(
        default_factory=lambda: {"APPROVED": 0},
    )
    rejected_exit: int = 3
    infrastructure_exit: int = 1
    git_adapter: Any = gitops
    final_md_builder: Callable[[Mapping[str, Any]], str] = _default_final_md
    finalize_callback: Callable[
        [Mapping[str, Any]], Mapping[str, Any]
    ] = _default_finalize
    payload_builder: Callable[
        [Mapping[str, Any]], Mapping[str, Any]
    ] | None = None
    final_writer: Callable[
        [str | os.PathLike[str], str, Mapping[str, Any]], Any
    ] = _default_blocked_writer
    post_write: Callable[[Mapping[str, Any]], None] | None = None
    post_write_fatal: bool = False
    state_complete: Callable[[Mapping[str, Any]], None] | None = None
    ci_exit_mapper: Callable[[Mapping[str, Any]], int] | None = None
    reporter: Callable[[str], None] = _stdout_report


def finish_pipeline(
    args: Any,
    workdir: str | os.PathLike[str],
    feature: str,
    out_dir: str | os.PathLike[str],
    state: MutableMapping[str, Any],
    verdict: str,
    reason: str = "",
    loops: int = 0,
    *,
    conditions: Sequence[str] | None = None,
    extra: Mapping[str, Any] | None = None,
    policy: FinishPolicy | None = None,
) -> int:
    """Finalize git and artifacts through explicit consumer policy callbacks."""
    selected = policy or FinishPolicy()
    context: dict[str, Any] = {
        "args": args,
        "workdir": workdir,
        "feature": feature,
        "out_dir": out_dir,
        "state": state,
        "verdict": verdict,
        "reason": reason,
        "loops": loops,
        "conditions": list(conditions or []),
        "extra": dict(extra or {}),
        "policy": selected,
    }
    try:
        _atomic_write_text(
            Path(out_dir) / "final.md", selected.final_md_builder(context),
        )
    except Exception as exc:
        _safe_report(selected.reporter, f"X final artifact write failed: {exc}")
        return selected.infrastructure_exit

    finalize_error = ""
    try:
        finalized = dict(selected.finalize_callback(context))
        if finalized.get("exit_code", 0) != 0 or finalized.get("error"):
            finalize_error = str(finalized.get("error", "unknown error"))
    except Exception as exc:
        finalized = {"exit_code": selected.infrastructure_exit, "merged": False}
        finalize_error = str(exc)
    if finalize_error:
        _safe_report(
            selected.reporter,
            f"X git finalize failed ({verdict}): {finalize_error}",
        )
    context["finalized"] = finalized
    context["finalize_error"] = finalize_error

    payload: dict[str, Any] = {
        "reason": reason,
        "loops": loops,
        "branch": state.get("branch", ""),
        "merged": bool(finalized.get("merged", False)),
        "artifacts_dir": str(out_dir),
        "conditions": list(conditions or []),
        **dict(extra or {}),
    }
    if finalize_error:
        payload["error"] = f"git finalize failed: {finalize_error}"
    if selected.payload_builder is not None:
        try:
            payload.update(dict(selected.payload_builder(context)))
        except Exception as exc:
            _safe_report(selected.reporter, f"X final payload build failed: {exc}")
            return selected.infrastructure_exit
    infrastructure = bool(finalize_error) or bool(
        payload.pop("infrastructure", False)
    )
    provider_history = payload.pop(
        "provider_history", state.get("provider_history", []),
    )
    payload.pop("verdict", None)
    try:
        normalized = runner.ensure_final_payload(
            verdict=verdict,
            infrastructure=infrastructure,
            provider_history=provider_history,
            **payload,
        )
    except Exception as exc:
        _safe_report(selected.reporter, f"X final payload normalization failed: {exc}")
        return selected.infrastructure_exit
    normalized.pop("verdict", None)
    context["final_payload"] = normalized
    try:
        selected.final_writer(out_dir, verdict, normalized)
    except Exception as exc:
        _safe_report(selected.reporter, f"X final artifact write failed: {exc}")
        return selected.infrastructure_exit

    if selected.post_write is not None:
        try:
            selected.post_write(context)
        except Exception as exc:
            _safe_report(selected.reporter, f"! post-write action failed: {exc}")
            if selected.post_write_fatal:
                return selected.infrastructure_exit
    if not finalize_error and selected.state_complete is not None:
        try:
            selected.state_complete(context)
        except Exception as exc:
            _safe_report(selected.reporter, f"X state completion failed: {exc}")
            return selected.infrastructure_exit

    _safe_report(selected.reporter, f"\n{verdict}" + (f" — {reason}" if reason else ""))
    if bool(getattr(args, "ci", False)):
        if selected.ci_exit_mapper is not None:
            try:
                return selected.ci_exit_mapper(context)
            except Exception as exc:
                _safe_report(selected.reporter, f"X CI exit mapping failed: {exc}")
                return selected.infrastructure_exit
        try:
            return runner.ci_exit_code(
                verdict,
                findings=normalized.get(
                    "finding_details", normalized.get("findings", []),
                ),
                fail_on_selector=getattr(args, "fail_on", None),
                infrastructure=bool(normalized.get("infrastructure")),
                context_blocked=bool(normalized.get("context_blocked")),
            )
        except Exception as exc:
            _safe_report(selected.reporter, f"X CI exit mapping failed: {exc}")
            return selected.infrastructure_exit
    if normalized.get("infrastructure"):
        return selected.infrastructure_exit
    return selected.exit_by_verdict.get(verdict, selected.rejected_exit)


def ci_exit_from_final(
    out_dir: str | os.PathLike[str],
    legacy_code: int,
    *,
    fail_on_selector: str | None = None,
    final_name: str = "final.json",
    error_writer: Callable[[Exception], None] | None = None,
    mapper: Callable[..., int] = runner.ci_exit_code,
) -> int:
    """Map a persisted final artifact to CI policy, preferring recorded metadata."""
    path = Path(out_dir) / final_name
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, Mapping):
            raise ValueError("final artifact must contain a JSON object")
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        if error_writer is not None:
            try:
                error_writer(exc)
            except Exception:
                pass
        return runner.CI_EXIT_INFRASTRUCTURE
    ci_metadata = payload.get("ci")
    if isinstance(ci_metadata, Mapping):
        recorded = ci_metadata.get("exit_code")
        if isinstance(recorded, int) and not isinstance(recorded, bool):
            return recorded
    findings = payload.get("finding_details")
    if not isinstance(findings, list):
        findings = payload.get("findings", [])
    try:
        return mapper(
            payload.get("verdict", "ERROR"),
            findings=findings,
            fail_on_selector=fail_on_selector,
            infrastructure=bool(payload.get("infrastructure")),
            context_blocked=bool(payload.get("context_blocked")),
        )
    except Exception:
        return legacy_code


def non_negative_int(value: Any) -> int:
    """Argparse type accepting integers greater than or equal to zero."""
    try:
        if isinstance(value, bool):
            raise ValueError
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise argparse.ArgumentTypeError(f"not an integer: {value!r}") from exc
    if parsed < 0:
        raise argparse.ArgumentTypeError(
            f"must be a non-negative integer, got {value!r}",
        )
    return parsed


def positive_int(value: Any) -> int:
    """Argparse type accepting strictly positive integers."""
    try:
        parsed = non_negative_int(value)
    except argparse.ArgumentTypeError:
        raise
    if parsed == 0:
        raise argparse.ArgumentTypeError(
            f"must be a positive integer, got {value!r}",
        )
    return parsed


__all__ = [
    "SETTLED_STATUSES",
    "FinishPolicy", "GitSetupPolicy", "PreflightPolicy", "PreflightResult",
    "RestorePolicy", "RestoreResult", "RetrospectivePolicy",
    "banner", "ci_exit_from_final", "ensure_finding_ids", "finish_pipeline",
    "is_settled_status", "log_retrospective", "non_negative_int",
    "phase_failure", "positive_int", "preflight", "record_phase",
    "restore_git", "setup_git", "threshold_overrides", "unresolved_findings",
    "write_json",
]
