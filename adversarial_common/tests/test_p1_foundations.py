"""Acceptance coverage for the P1 shared foundations."""

import sys
import time

from adversarial_common.costs import CostLedger
from adversarial_common.gates import post_build_gate, post_fix_gate, pre_build_gate
from adversarial_common.jsonio import epistemic_distribution, parse_json_output
from adversarial_common.runner import run_cli, run_parallel


def test_cost_ledger_native_and_estimated_usage():
    ledger = CostLedger(prices={"test": {"prompt": 1, "completion": 2}})
    native = ledger.record(
        "test", usage={"input_tokens": 10, "output_tokens": 5}, phase="review"
    )
    estimated = ledger.record(
        "test", prompt_text="abcdefgh", completion_text="abcd", phase="fix"
    )
    assert native.estimated is False
    assert estimated.estimated is True
    summary = ledger.summary()
    assert summary["models"]["test"]["prompt_tokens"] == 12
    assert summary["models"]["test"]["completion_tokens"] == 6
    assert summary["total"]["est_cost_usd"] == summary["models"]["test"]["est_cost_usd"]


def test_runner_threads_usage_to_ledger():
    ledger = CostLedger()
    result = run_cli(
        [sys.executable, "-c", "print('done')"],
        stdin_text="prompt",
        ledger=ledger,
        model="unknown-test-model",
        phase="build",
        persona="builder",
        include_usage=True,
    )
    assert result[:3] == ("done", "", 0)
    assert result[3]["estimated"] is True
    assert ledger.summary()["records"][0]["phase"] == "build"


def test_run_parallel_is_bounded_ordered_and_isolates_failure():
    sleeper = [sys.executable, "-c", "import time; time.sleep(.15); print('ok')"]
    started = time.monotonic()
    results = run_parallel([
        ("first", {"cmd": sleeper}),
        ("bad", {"cmd": ["definitely-not-a-command"]}),
        ("third", {"cmd": sleeper}),
    ], concurrency=2)
    elapsed = time.monotonic() - started
    assert [result["label"] for result in results] == ["first", "bad", "third"]
    assert [result["ok"] for result in results] == [True, False, True]
    assert elapsed < 0.42


def test_verification_gates_are_structured_and_bounded(tmp_path):
    (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n")
    pre = pre_build_gate(tmp_path, [sys.executable, "-c", "pass"])
    assert pre["ok"] is True
    missing = pre_build_gate(tmp_path, ["definitely-not-a-command"])
    assert missing["ok"] is False and missing["exit_code"] == 127
    passed = post_build_gate(tmp_path, [sys.executable, "-c", "print('ok')"])
    assert passed["ok"] is True and passed["command"]
    failed = post_fix_gate(
        tmp_path,
        [sys.executable, "-c", "print('x' * 100); raise SystemExit(4)"],
        max_log_chars=30,
    )
    assert failed["exit_code"] == 4
    assert failed["truncated"] is True and len(failed["log"]) == 30


def test_epistemic_labels_default_with_warning_and_distribution():
    payload = parse_json_output('{"findings":[{"id":"A1"},{"id":"A2","confidence":"high","basis":"code"}]}')
    first = payload["findings"][0]
    assert (first["confidence"], first["basis"]) == ("low", "inference")
    assert payload["warnings"][0]["code"] == "epistemic_label_defaulted"
    distribution = epistemic_distribution(payload["findings"])
    assert distribution["combined"] == {"low/inference": 1, "high/code": 1}
