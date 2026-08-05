"""Strict-consensus vote-evaluation module (pure logic)."""

from dataclasses import dataclass


@dataclass(frozen=True)
class ConsensusResult:
    accepted: bool
    votes: dict[str, bool]
    reason: str


def strict_consensus(votes: dict[str, bool], strict: bool = False) -> ConsensusResult:
    # ponytail: dict() copy prevents caller mutation from desynchronizing result fields
    if not votes:
        return ConsensusResult(accepted=False, votes={}, reason="no votes")

    if strict:
        for model_id, accepted in votes.items():
            if not accepted:
                return ConsensusResult(
                    accepted=False,
                    votes=dict(votes),
                    reason=f"no consensus — {model_id} dissented",
                )
        return ConsensusResult(accepted=True, votes=dict(votes), reason="consensus — all models accepted")

    any_accept = any(votes.values())
    if any_accept:
        return ConsensusResult(accepted=True, votes=dict(votes), reason="at least one model accepted")
    return ConsensusResult(accepted=False, votes=dict(votes), reason="no model accepted")
