"""Unit tests for strict_consensus (P4 — strict-consensus mode)."""

from adversarial_common.consensus import strict_consensus, ConsensusResult


class TestStrictConsensus:
    # AC1: strict=True, both accept → accepted=True, reason indicates consensus
    def test_strict_both_accept(self):
        result = strict_consensus(
            votes={"model-a": True, "model-b": True},
            strict=True,
        )
        assert isinstance(result, ConsensusResult)
        assert result.accepted is True
        assert result.votes == {"model-a": True, "model-b": True}
        assert "consensus" in result.reason.lower()

    # AC2: strict=True, one dissents → accepted=False, reason contains "no consensus" and model id
    def test_strict_one_dissents(self):
        result = strict_consensus(
            votes={"model-a": True, "model-b": False},
            strict=True,
        )
        assert result.accepted is False
        assert result.votes == {"model-a": True, "model-b": False}
        assert "no consensus" in result.reason.lower()
        assert "model-b" in result.reason

    # AC3: strict=False, one accepts, one dissents → accepted=True
    def test_non_strict_one_accepts(self):
        result = strict_consensus(
            votes={"model-a": False, "model-b": True},
            strict=False,
        )
        assert result.accepted is True

    # AC4: strict=False, both dissent → accepted=False
    def test_non_strict_both_dissent(self):
        result = strict_consensus(
            votes={"model-a": False, "model-b": False},
            strict=False,
        )
        assert result.accepted is False


class TestEdgeCases:
    def test_strict_single_model_accept(self):
        result = strict_consensus(
            votes={"solo": True},
            strict=True,
        )
        assert result.accepted is True

    def test_strict_single_model_dissent(self):
        result = strict_consensus(
            votes={"solo": False},
            strict=True,
        )
        assert result.accepted is False
        assert "solo" in result.reason

    def test_non_strict_single_accept(self):
        result = strict_consensus(
            votes={"solo": True},
            strict=False,
        )
        assert result.accepted is True

    def test_non_strict_single_dissent(self):
        result = strict_consensus(
            votes={"solo": False},
            strict=False,
        )
        assert result.accepted is False

    def test_empty_votes(self):
        result = strict_consensus(votes={}, strict=True)
        assert result.accepted is False
        assert result.votes == {}
        assert "no votes" in result.reason.lower()

    def test_three_models_strict_one_dissents(self):
        result = strict_consensus(
            votes={"a": True, "b": True, "c": False},
            strict=True,
        )
        assert result.accepted is False
        assert "c" in result.reason

    def test_three_models_strict_all_accept(self):
        result = strict_consensus(
            votes={"a": True, "b": True, "c": True},
            strict=True,
        )
        assert result.accepted is True

    def test_three_models_non_strict_one_accepts(self):
        result = strict_consensus(
            votes={"a": False, "b": True, "c": False},
            strict=False,
        )
        assert result.accepted is True

    def test_three_models_non_strict_all_dissent(self):
        result = strict_consensus(
            votes={"a": False, "b": False, "c": False},
            strict=False,
        )
        assert result.accepted is False
