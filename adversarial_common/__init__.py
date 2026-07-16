"""Shared engine for the adversarial skill suite."""

from pathlib import Path

from .costs import (
    MODEL_PRICES,
    PROVIDER_PRICE_ALIASES,
    BudgetReservation,
    CostLedger,
    UsageRecord,
    estimate_tokens,
)
from .gates import (
    TRUNCATION_MARKER,
    check_context,
    enforce_input_cap,
    enforce_output_cap,
    estimate_complexity,
    post_build_gate,
    post_fix_gate,
    pre_build_gate,
)
from .jsonio import (
    VALID_BASIS,
    VALID_CONFIDENCE,
    epistemic_distribution,
    extract_frontmatter,
    normalize_findings,
    parse_frontmatter,
    parse_json_output,
    resume_artifact,
    save_artifact,
    strip_json_wrapper,
    write_final_json,
)
from .providers import (
    DEFAULT_PROVIDER_CONFIG_PATH,
    PROVIDER_CONFIG_ENV,
    ProviderConfig,
    ProviderConfigError,
    ProviderEntry,
    ProviderRegistry,
    classify_transient_error,
    default_wrapper_cmd,
    detect_provider,
    enhance_cmd_for_project,
    extract_usage_metadata,
    inject_persona,
    is_transient_error,
    load_provider_config,
    persona_for_role,
    resolve_provider_config_path,
    resolve_role_cmd,
    run_cmd,
)
from .quota import (
    DRAINING,
    KEY_INVALID,
    OK,
    PROVIDER_STATES,
    RATE_LIMITED,
    UNKNOWN,
    NoProviderAvailable,
    ProviderDecision,
    QuotaResolver,
)
from .report import render_html_report, render_report
from .runner import (
    CI_EXIT_BLOCKING,
    CI_EXIT_CLEAN,
    CI_EXIT_CONTEXT_BLOCKED,
    CI_EXIT_INFRASTRUCTURE,
    CI_EXIT_NON_BLOCKING,
    COST_BUDGET_EXIT_CODE,
    DEFAULT_MAX_INPUT_CHARS,
    DEFAULT_MAX_OUTPUT_CHARS,
    DEFAULT_RESEARCH_MAX_INPUT_CHARS,
    DEFAULT_RESEARCH_MAX_OUTPUT_CHARS,
    DEFAULT_RESEARCH_MAX_QUERIES,
    DEFAULT_RESEARCH_MAX_RESULTS,
    DEFAULT_RESEARCH_TIMEOUT,
    RunResult,
    build_final_payload,
    ci_exit_code,
    ci_mode,
    ci_print,
    ensure_final_payload,
    fail_phase,
    parse_fail_on,
    run_cli,
    run_delegated,
    run_parallel,
    run_research,
)
from .snapshot import snapshot_workdir


PERSONAS_DIR = Path(__file__).resolve().parent.parent / "personas"


def persona_path(name):
    """Return the absolute path to a named persona file."""
    path = PERSONAS_DIR / f"{name}.md"
    if not path.is_file():
        raise FileNotFoundError(f"Persona not found: {path}")
    return str(path)


def load_persona(name):
    """Return the text of a named persona."""
    return Path(persona_path(name)).read_text()


__all__ = [
    # costs
    "BudgetReservation", "CostLedger", "MODEL_PRICES",
    "PROVIDER_PRICE_ALIASES", "UsageRecord", "estimate_tokens",
    # gates
    "TRUNCATION_MARKER", "check_context", "enforce_input_cap",
    "enforce_output_cap", "estimate_complexity", "post_build_gate",
    "post_fix_gate", "pre_build_gate",
    # jsonio
    "VALID_BASIS", "VALID_CONFIDENCE", "epistemic_distribution",
    "extract_frontmatter", "normalize_findings", "parse_frontmatter",
    "parse_json_output", "resume_artifact", "save_artifact",
    "strip_json_wrapper", "write_final_json",
    # providers
    "DEFAULT_PROVIDER_CONFIG_PATH", "PROVIDER_CONFIG_ENV", "ProviderConfig",
    "ProviderConfigError", "ProviderEntry", "ProviderRegistry",
    "classify_transient_error", "default_wrapper_cmd", "detect_provider",
    "enhance_cmd_for_project", "extract_usage_metadata", "inject_persona",
    "is_transient_error", "load_provider_config", "persona_for_role",
    "resolve_provider_config_path", "resolve_role_cmd", "run_cmd",
    # quota
    "DRAINING", "KEY_INVALID", "NoProviderAvailable", "OK",
    "PROVIDER_STATES", "ProviderDecision", "QuotaResolver", "RATE_LIMITED",
    "UNKNOWN",
    # report
    "render_html_report", "render_report",
    # runner
    "CI_EXIT_BLOCKING", "CI_EXIT_CLEAN", "CI_EXIT_CONTEXT_BLOCKED",
    "CI_EXIT_INFRASTRUCTURE", "CI_EXIT_NON_BLOCKING",
    "COST_BUDGET_EXIT_CODE",
    "DEFAULT_MAX_INPUT_CHARS", "DEFAULT_MAX_OUTPUT_CHARS",
    "DEFAULT_RESEARCH_MAX_INPUT_CHARS", "DEFAULT_RESEARCH_MAX_OUTPUT_CHARS",
    "DEFAULT_RESEARCH_MAX_QUERIES", "DEFAULT_RESEARCH_MAX_RESULTS",
    "DEFAULT_RESEARCH_TIMEOUT",
    "RunResult", "build_final_payload", "ci_exit_code", "ci_mode",
    "ci_print", "ensure_final_payload", "fail_phase", "parse_fail_on",
    "run_cli", "run_delegated", "run_parallel", "run_research",
    # snapshot
    "snapshot_workdir",
    # personas
    "PERSONAS_DIR", "load_persona", "persona_path",
]
