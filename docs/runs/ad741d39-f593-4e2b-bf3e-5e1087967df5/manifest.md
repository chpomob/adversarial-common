---
run_id: ad741d39-f593-4e2b-bf3e-5e1087967df5
feature: readgate
models:
  builder:
  - deepseek-v4-pro
  critic:
  - other
  fixer:
  - deepseek-v4-pro
  verifier:
  - other
started: '2026-08-07T10:18:27.942142+00:00'
finished: '2026-08-07T10:23:33.444901+00:00'
duration_s: 305.5
findings:
  total: 4
  accepted: 2
  rejected: 0
verdict: APPROVED
verdicts_per_round:
- round: 1
  verdict: APPROVE
costs:
  total_tokens: 4144
  est_cost_usd: 0.001142
---

# Run Manifest — readgate

- **Run ID**: `ad741d39-f593-4e2b-bf3e-5e1087967df5`
- **Verdict**: APPROVED
- **Duration**: 305.5s
- **Findings**: 4 total (2 accepted, 0 rejected)

## Models Used
- **builder**: deepseek-v4-pro
- **critic**: other
- **fixer**: deepseek-v4-pro
- **verifier**: other

## Verdicts Per Round
- Round 1: APPROVE

## Costs (estimated)
- Total tokens: 4144
- Estimated cost: $0.001142 USD
