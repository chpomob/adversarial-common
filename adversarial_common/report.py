"""Render self-contained Markdown and HTML adversarial run reports."""

import json
import sys
from html import escape
from pathlib import Path


_REPORT_NAME = "report.html"
_MARKDOWN_REPORT_NAME = "final.md"
_ALLOWED_ARTIFACT_SUFFIXES = frozenset({".err", ".json", ".md", ".txt"})
_MAX_ARTIFACTS = 100
_MAX_ARTIFACT_CHARS = 128 * 1024
_MAX_TOTAL_ARTIFACT_CHARS = 1024 * 1024
_VALID_CONFIDENCE = frozenset({"high", "medium", "low"})
_VALID_BASIS = frozenset({"spec", "code", "inference", "external"})


def render_report(final_json, artifacts=None):
    """Write ``final.md`` and ``report.html`` next to *final_json*.

    The HTML path is returned for compatibility with the original reporting
    API.  The Markdown artifact is always generated from the same validated
    payload and provider history.

    ``artifacts`` may be omitted to discover numbered phase artifacts (for
    example ``01_architect.txt``), supplied as an iterable of paths, or supplied
    as a mapping of display names to raw text.  Discovery is intentionally
    narrow so an artifacts directory cannot accidentally expose unrelated
    files in the report.

    Invalid JSON and non-object root values are rejected.  Every field below
    the root is optional and malformed optional values are rendered safely.
    """
    final_path, payload = _load_final_payload(final_json)
    raw_artifacts = _load_artifacts(final_path, artifacts)
    provider_history = _provider_history(payload.get("provider_history"))
    markdown_path = final_path.with_name(_MARKDOWN_REPORT_NAME)
    report_path = final_path.with_name(_REPORT_NAME)
    markdown_path.write_text(
        _render_markdown_document(
            payload, final_path, raw_artifacts, provider_history
        ),
        encoding="utf-8",
    )
    document = _render_document(
        payload, final_path, report_path, raw_artifacts, provider_history
    )
    report_path.write_text(document, encoding="utf-8")
    return report_path


def render_html_report(final_json, artifacts=None):
    """Write and return the self-contained HTML report."""
    final_path, payload = _load_final_payload(final_json)
    raw_artifacts = _load_artifacts(final_path, artifacts)
    provider_history = _provider_history(payload.get("provider_history"))
    report_path = final_path.with_name(_REPORT_NAME)
    report_path.write_text(
        _render_document(
            payload, final_path, report_path, raw_artifacts, provider_history
        ),
        encoding="utf-8",
    )
    return report_path


def _load_final_payload(final_json):
    final_path = Path(final_json)
    try:
        payload = json.loads(final_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid final JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError("final JSON must contain an object")
    return final_path, payload


def _load_artifacts(final_path, artifacts):
    if artifacts is None:
        candidates = [
            path for path in final_path.parent.iterdir()
            if _is_discoverable_artifact(path)
        ]
        return _read_artifact_paths(final_path, sorted(candidates))

    if isinstance(artifacts, dict):
        entries = []
        total = 0
        for name, content in sorted(artifacts.items(), key=lambda item: str(item[0])):
            if len(entries) >= _MAX_ARTIFACTS or total >= _MAX_TOTAL_ARTIFACT_CHARS:
                break
            text = _as_text(content)
            remaining = _MAX_TOTAL_ARTIFACT_CHARS - total
            bounded = _bounded_text(text, min(_MAX_ARTIFACT_CHARS, remaining))
            entries.append((str(name), bounded))
            total += len(bounded)
        return entries

    if isinstance(artifacts, (str, Path)):
        artifacts = [artifacts]
    return _read_artifact_paths(final_path, artifacts)


def _is_discoverable_artifact(path):
    name = path.name
    return (
        path.is_file()
        and not path.is_symlink()
        and len(name) > 3
        and name[:2].isdigit()
        and name[2] == "_"
        and path.suffix.lower() in _ALLOWED_ARTIFACT_SUFFIXES
    )


def _read_artifact_paths(final_path, artifacts):
    directory = final_path.parent.resolve()
    entries = []
    total = 0
    for item in artifacts:
        if len(entries) >= _MAX_ARTIFACTS or total >= _MAX_TOTAL_ARTIFACT_CHARS:
            break
        candidate = Path(item)
        if not candidate.is_absolute():
            candidate = final_path.parent / candidate
        try:
            contained = candidate.parent.resolve() == directory
        except OSError:
            contained = False
        if (
            not contained
            or candidate.name in {final_path.name, _REPORT_NAME}
            or candidate.is_symlink()
            or not candidate.is_file()
        ):
            continue
        try:
            text = candidate.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            text = f"[artifact unavailable: {exc}]"
        remaining = _MAX_TOTAL_ARTIFACT_CHARS - total
        bounded = _bounded_text(text, min(_MAX_ARTIFACT_CHARS, remaining))
        entries.append((candidate.name, bounded))
        total += len(bounded)
    return entries


def _bounded_text(text, limit):
    marker = "\n\n[output truncated by report renderer]"
    if limit <= 0:
        return ""
    if len(text) <= limit:
        return text
    if limit <= len(marker):
        return marker[:limit]
    return text[:limit - len(marker)] + marker


def _render_document(
    payload, final_path, report_path, artifacts, provider_history
):
    verdict = payload.get("verdict", payload.get("status", "UNKNOWN"))
    verdict_text = _as_text(verdict) if _is_scalar(verdict) else "UNKNOWN"
    status_class = _status_class(verdict_text)

    sections = [
        _render_overview(payload, final_path, report_path),
        _render_findings(payload.get("finding_details", payload.get("findings"))),
        _render_epistemic(payload),
        _render_costs(payload.get("costs")),
        _render_execution(payload),
        _render_provider_history_html(provider_history),
        _render_gates(payload.get("gates")),
        _render_warnings(payload.get("warnings")),
        _render_artifacts(artifacts),
    ]
    title = f"Adversarial report — {verdict_text}"
    return """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'">
<title>{title}</title>
<style>
:root {{ color-scheme: light dark; --bg:#f5f7fb; --panel:#fff; --text:#172033;
  --muted:#596579; --line:#d9dfeb; --good:#16794b; --bad:#b42318;
  --warn:#a15c00; --accent:#3157c8; }}
@media (prefers-color-scheme: dark) {{ :root {{ --bg:#111522; --panel:#1b2130;
  --text:#edf1f8; --muted:#aab4c5; --line:#343e52; --good:#55d69e;
  --bad:#ff8c82; --warn:#ffc36d; --accent:#91a9ff; }} }}
* {{ box-sizing:border-box; }}
body {{ margin:0; background:var(--bg); color:var(--text); font:15px/1.55
  system-ui,-apple-system,"Segoe UI",sans-serif; }}
main {{ width:min(1100px,calc(100% - 32px)); margin:32px auto 64px; }}
h1,h2,h3 {{ line-height:1.2; }} h1 {{ margin-bottom:8px; }}
h2 {{ margin-top:0; font-size:1.2rem; }} h3 {{ font-size:1rem; }}
.panel {{ margin:16px 0; padding:20px; background:var(--panel); border:1px solid
  var(--line); border-radius:10px; box-shadow:0 2px 8px #0000000d; }}
.verdict {{ display:inline-block; margin:8px 0; padding:7px 12px; border:2px solid;
  border-radius:999px; font-weight:750; letter-spacing:.03em; }}
.verdict.good {{ color:var(--good); }} .verdict.bad {{ color:var(--bad); }}
.verdict.warn {{ color:var(--warn); }} .verdict.neutral {{ color:var(--accent); }}
.muted {{ color:var(--muted); }} .grid {{ display:grid;
  grid-template-columns:repeat(auto-fit,minmax(230px,1fr)); gap:12px; }}
.card {{ padding:14px; border:1px solid var(--line); border-radius:8px; }}
.badge {{ display:inline-block; margin:2px 5px 2px 0; padding:2px 8px;
  border:1px solid var(--line); border-radius:999px; font-size:.82rem; }}
dl {{ display:grid; grid-template-columns:minmax(100px,180px) 1fr; gap:6px 14px; }}
dt {{ color:var(--muted); font-weight:650; overflow-wrap:anywhere; }}
dd {{ margin:0; overflow-wrap:anywhere; white-space:pre-wrap; }}
table {{ width:100%; border-collapse:collapse; }} th,td {{ padding:8px 10px;
  text-align:left; border-bottom:1px solid var(--line); overflow-wrap:anywhere; }}
th {{ color:var(--muted); }} code,pre {{ font-family:ui-monospace,SFMono-Regular,
  Consolas,monospace; }} code {{ overflow-wrap:anywhere; }}
details {{ margin:10px 0; border:1px solid var(--line); border-radius:8px; }}
summary {{ padding:10px 12px; cursor:pointer; font-weight:650; }}
pre {{ max-height:38rem; margin:0; padding:14px; border-top:1px solid var(--line);
  overflow:auto; white-space:pre-wrap; overflow-wrap:anywhere; }}
.empty {{ color:var(--muted); font-style:italic; }}
</style>
</head>
<body>
<main>
<header>
<h1>Adversarial run report</h1>
<div class="verdict {status_class}">{verdict}</div>
</header>
{sections}
</main>
</body>
</html>
""".format(
        title=_escaped(title),
        status_class=status_class,
        verdict=_escaped(verdict_text),
        sections="\n".join(sections),
    )


def _render_overview(payload, final_path, report_path):
    excluded = {
        "costs", "delegated", "epistemic_distribution", "epistemic_labels",
        "finding_details", "findings", "gates", "parallel",
        "provider_history", "warnings",
    }
    rows = [
        (key, value) for key, value in payload.items()
        if key not in excluded and key not in {"verdict", "status"}
    ]
    metadata = _definition_list(rows) if rows else '<p class="empty">No additional run metadata.</p>'
    return """<section class="panel">
<h2>Run overview</h2>
<p class="muted">Source artifact: <code>{source}</code><br>
HTML artifact: <code>{report}</code></p>
{metadata}
</section>""".format(
        source=_escaped(str(final_path)),
        report=_escaped(str(report_path)),
        metadata=metadata,
    )


def _render_findings(findings):
    if findings is None:
        body = '<p class="empty">No findings were recorded.</p>'
    elif isinstance(findings, list):
        cards = []
        for index, finding in enumerate(findings):
            if not isinstance(finding, dict):
                cards.append(f'<article class="card"><pre>{_escaped(_json_text(finding))}</pre></article>')
                continue
            confidence = finding.get("confidence")
            basis = finding.get("basis")
            if confidence not in _VALID_CONFIDENCE or basis not in _VALID_BASIS:
                confidence, basis = "low", "inference"
            heading = finding.get("id", f"Finding {index + 1}")
            severity = finding.get("severity", "unspecified")
            rows = [
                (key, value) for key, value in finding.items()
                if key not in {"id", "severity", "confidence", "basis"}
            ]
            cards.append("""<article class="card">
<h3>{heading}</h3>
<span class="badge">severity: {severity}</span>
<span class="badge">confidence: {confidence}</span>
<span class="badge">basis: {basis}</span>
{fields}
</article>""".format(
                heading=_escaped(heading), severity=_escaped(severity),
                confidence=_escaped(confidence), basis=_escaped(basis),
                fields=_definition_list(rows),
            ))
        body = '<div class="grid">' + "".join(cards) + "</div>" if cards else '<p class="empty">The findings list is empty.</p>'
    elif isinstance(findings, dict):
        body = _mapping_table(findings, "Category", "Count")
    else:
        body = f'<pre>{_escaped(_json_text(findings))}</pre>'
    return f'<section class="panel"><h2>Findings</h2>{body}</section>'


def _render_epistemic(payload):
    labels = payload.get("epistemic_labels", payload.get("epistemic_distribution"))
    if labels is None:
        body = '<p class="empty">No epistemic distribution was recorded.</p>'
    elif isinstance(labels, dict):
        groups = []
        for label, values in labels.items():
            if isinstance(values, dict):
                content = _mapping_table(values, "Label", "Count")
            else:
                content = f'<pre>{_escaped(_json_text(values))}</pre>'
            groups.append(f'<div class="card"><h3>{_escaped(label)}</h3>{content}</div>')
        body = '<div class="grid">' + "".join(groups) + "</div>"
    else:
        body = f'<pre>{_escaped(_json_text(labels))}</pre>'
    return f'<section class="panel"><h2>Epistemic labels</h2>{body}</section>'


def _render_costs(costs):
    if costs is None:
        body = '<p class="empty">No cost data was recorded.</p>'
    elif isinstance(costs, dict):
        groups = []
        for label, values in costs.items():
            if label == "records" and isinstance(values, list):
                content = f'<details><summary>{len(values)} usage record(s)</summary><pre>{_escaped(_json_text(values))}</pre></details>'
            elif isinstance(values, dict):
                content = _nested_mapping_table(values)
            else:
                content = f'<pre>{_escaped(_json_text(values))}</pre>'
            groups.append(f'<div class="card"><h3>{_escaped(label)}</h3>{content}</div>')
        body = '<div class="grid">' + "".join(groups) + "</div>"
    else:
        body = f'<pre>{_escaped(_json_text(costs))}</pre>'
    return f'<section class="panel"><h2>Costs</h2>{body}</section>'


def _render_execution(payload):
    execution = {
        key: payload[key]
        for key in ("complexity", "parallel", "delegated")
        if payload.get(key) is not None
    }
    if not execution:
        body = "<p class=\"empty\">No execution metadata was recorded.</p>"
    else:
        cards = [
            "<div class=\"card\"><h3>{}</h3>{}</div>".format(
                _escaped(label),
                _definition_list(value.items())
                if isinstance(value, dict)
                else f"<pre>{_escaped(_json_text(value))}</pre>",
            )
            for label, value in execution.items()
        ]
        body = "<div class=\"grid\">" + "".join(cards) + "</div>"
    return f"<section class=\"panel\"><h2>Execution</h2>{body}</section>"


def _render_provider_history_html(history):
    if not history:
        body = '<p class="empty">No provider history recorded.</p>'
    else:
        rows = "".join(
            "<tr><td>{}</td><td>{}</td><td>{}</td><td>{}</td>"
            "<td>{}</td><td>{}</td></tr>".format(
                _escaped(entry["phase"]),
                _escaped(entry["alias"]),
                _escaped(entry["quota_state"]),
                "Yes" if entry["fallback"] else "No",
                "Yes" if entry["forced"] else "No",
                _escaped(entry["reason"]),
            )
            for entry in history
        )
        body = (
            "<table><thead><tr><th>Phase</th><th>Provider</th>"
            "<th>State</th><th>Fallback</th><th>Forced</th>"
            "<th>Reason</th></tr></thead>"
            f"<tbody>{rows}</tbody></table>"
        )
    return (
        '<section class="panel"><h2>Provider history</h2>'
        f"{body}</section>"
    )


def _render_gates(gates):
    if gates is None:
        body = '<p class="empty">No gate results were recorded.</p>'
    else:
        gate_items = gates if isinstance(gates, list) else [gates]
        cards = []
        for index, gate in enumerate(gate_items):
            if isinstance(gate, dict):
                heading = gate.get("gate", gate.get("name", f"Gate {index + 1}"))
                cards.append(f'<article class="card"><h3>{_escaped(heading)}</h3>{_definition_list(gate.items())}</article>')
            else:
                cards.append(f'<article class="card"><pre>{_escaped(_json_text(gate))}</pre></article>')
        body = '<div class="grid">' + "".join(cards) + "</div>" if cards else '<p class="empty">The gate list is empty.</p>'
    return f'<section class="panel"><h2>Verification gates</h2>{body}</section>'


def _render_warnings(warnings):
    if warnings is None:
        body = '<p class="empty">No warnings were recorded.</p>'
    else:
        warning_items = warnings if isinstance(warnings, list) else [warnings]
        cards = []
        for warning in warning_items:
            if isinstance(warning, dict):
                cards.append(f'<article class="card">{_definition_list(warning.items())}</article>')
            else:
                cards.append(f'<article class="card"><p>{_escaped(warning)}</p></article>')
        body = '<div class="grid">' + "".join(cards) + "</div>" if cards else '<p class="empty">The warning list is empty.</p>'
    return f'<section class="panel"><h2>Warnings</h2>{body}</section>'


def _render_artifacts(artifacts):
    if artifacts:
        body = "".join(
            f'<details><summary>{_escaped(name)}</summary><pre>{_escaped(content)}</pre></details>'
            for name, content in artifacts
        )
    else:
        body = '<p class="empty">No numbered phase artifacts were found.</p>'
    return f'<section class="panel"><h2>Raw phase outputs</h2>{body}</section>'


def _provider_history(value):
    """Validate report-facing history and omit malformed entries safely."""
    if value is None:
        return []
    if not isinstance(value, list):
        _warn_invalid_provider_history("provider_history must be an array")
        return []

    history = []
    for index, entry in enumerate(value):
        problem = _provider_history_problem(entry)
        if problem is not None:
            _warn_invalid_provider_history(
                f"provider_history entry {index + 1} skipped: {problem}"
            )
            continue
        history.append({
            "phase": entry["phase"],
            "alias": entry.get("alias"),
            "quota_state": entry["quota_state"],
            "fallback": entry["fallback"],
            "forced": entry["forced"],
            "reason": entry["reason"],
        })
    return history


def _provider_history_problem(entry):
    if not isinstance(entry, dict):
        return "entry must be an object"
    required_types = {
        "phase": str,
        "quota_state": str,
        "fallback": bool,
        "forced": bool,
        "reason": str,
    }
    for key, expected in required_types.items():
        if key not in entry or type(entry[key]) is not expected:
            return f"missing or invalid '{key}'"
    if entry.get("alias") is not None and type(entry["alias"]) is not str:
        return "invalid 'alias'"
    if "raw_snapshot" in entry and not isinstance(entry["raw_snapshot"], dict):
        return "invalid 'raw_snapshot'"
    return None


def _warn_invalid_provider_history(message):
    print(f"Provider history validation warning: {message}.", file=sys.stderr)


def _render_markdown_document(payload, final_path, artifacts, history):
    verdict = payload.get("verdict", payload.get("status", "UNKNOWN"))
    verdict_text = _as_text(verdict) if _is_scalar(verdict) else "UNKNOWN"
    lines = [
        "# Adversarial run report",
        "",
        f"Verdict: **{_markdown_cell(verdict_text)}**",
        "",
        "## Run overview",
        "",
        f"Source artifact: `{_markdown_code(final_path)}`",
    ]
    summary = payload.get("summary")
    if _is_scalar(summary) and summary is not None:
        lines.extend(["", f"Summary: {_markdown_cell(summary)}"])

    lines.extend(["", "## Provider history", ""])
    if history:
        lines.extend([
            "| Phase | Provider | State | Fallback | Forced | Reason |",
            "| --- | --- | --- | --- | --- | --- |",
        ])
        for entry in history:
            lines.append(
                "| {} | {} | {} | {} | {} | {} |".format(
                    _markdown_cell(entry["phase"]),
                    _markdown_cell(entry["alias"]),
                    _markdown_cell(entry["quota_state"]),
                    "Yes" if entry["fallback"] else "No",
                    "Yes" if entry["forced"] else "No",
                    _markdown_cell(entry["reason"]),
                )
            )
    else:
        lines.append("No provider history recorded.")

    lines.extend(["", "## Findings", ""])
    findings = payload.get("finding_details", payload.get("findings"))
    if not findings:
        lines.append("No findings were recorded.")
    elif isinstance(findings, list):
        for index, finding in enumerate(findings):
            if isinstance(finding, dict):
                heading = finding.get("id", f"Finding {index + 1}")
                detail = finding.get("summary", finding.get("message", ""))
                lines.append(
                    f"- **{_markdown_cell(heading)}:** {_markdown_cell(detail)}"
                )
            else:
                lines.append(f"- {_markdown_cell(_display_text(finding))}")
    else:
        lines.append(_markdown_cell(_display_text(findings)))

    lines.extend(["", "## Raw phase outputs", ""])
    if artifacts:
        for name, content in artifacts:
            lines.extend([
                f"### {_markdown_cell(name)}",
                "",
                "```text",
                _markdown_fenced_text(content),
                "```",
                "",
            ])
    else:
        lines.append("No numbered phase artifacts were found.")
    return "\n".join(lines).rstrip() + "\n"


def _markdown_cell(value):
    text = _as_text(value)
    return (
        escape(text, quote=False)
        .replace("\\", "\\\\")
        .replace("|", "\\|")
        .replace("\r\n", "<br>")
        .replace("\r", "<br>")
        .replace("\n", "<br>")
    )


def _markdown_code(value):
    return _as_text(value).replace("`", "\\`").replace("\n", " ")


def _markdown_fenced_text(value):
    # Keep untrusted artifact text inside the fence even when it contains a
    # same-length closing delimiter.
    return _as_text(value).replace("```", "` ` `")


def _definition_list(items):
    parts = []
    for key, value in items:
        parts.append(f'<dt>{_escaped(key)}</dt><dd>{_escaped(_display_text(value))}</dd>')
    return "<dl>" + "".join(parts) + "</dl>" if parts else ""


def _mapping_table(mapping, first_heading, second_heading):
    rows = "".join(
        f'<tr><td>{_escaped(key)}</td><td>{_escaped(_display_text(value))}</td></tr>'
        for key, value in mapping.items()
    )
    return f'<table><thead><tr><th>{first_heading}</th><th>{second_heading}</th></tr></thead><tbody>{rows}</tbody></table>'


def _nested_mapping_table(mapping):
    if all(not isinstance(value, (dict, list)) for value in mapping.values()):
        return _mapping_table(mapping, "Metric", "Value")
    rows = "".join(
        f'<tr><td>{_escaped(key)}</td><td>{_escaped(_display_text(value))}</td></tr>'
        for key, value in mapping.items()
    )
    return f'<table><thead><tr><th>Group</th><th>Usage</th></tr></thead><tbody>{rows}</tbody></table>'


def _display_text(value):
    return _as_text(value) if _is_scalar(value) else _json_text(value)


def _json_text(value):
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True)


def _is_scalar(value):
    return value is None or isinstance(value, (str, int, float, bool))


def _as_text(value):
    if value is None:
        return "—"
    if value is True:
        return "true"
    if value is False:
        return "false"
    return str(value)


def _escaped(value):
    """Escape all dynamic content, including quotes used in future attributes."""
    return escape(_as_text(value), quote=True)


def _status_class(verdict):
    normalized = verdict.strip().upper()
    if normalized in {"APPROVE", "APPROVED", "ARBITRATED", "CLEAN", "PASS", "PASSED"}:
        return "good"
    if normalized in {"REJECT", "REJECTED", "ERROR", "FAIL", "FAILED", "BLOCKED"}:
        return "bad"
    if normalized in {"REQUEST_CHANGES", "WARN", "WARNING"}:
        return "warn"
    return "neutral"


__all__ = ["render_html_report", "render_report"]
