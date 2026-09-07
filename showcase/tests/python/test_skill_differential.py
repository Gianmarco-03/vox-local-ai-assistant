"""I casi differenziali condivisi con il runtime .NET: stesso input, stesso
atteso scritto a mano in protocol-cases/skills/. Se un runtime diverge, uno
dei due test (questo o CalculateDifferentialTests in C#) diventa rosso."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from skills.calculate import CalculateSkill

CASES = json.loads(
    (Path(__file__).resolve().parents[1] / "protocol-cases" / "skills" /
     "calculate.json").read_text(encoding="utf-8"))["cases"]


@pytest.mark.parametrize("case", CASES, ids=[c["name"] for c in CASES])
def test_calculate_matches_the_shared_expectations(case):
    result = CalculateSkill().execute(**case["args"])
    assert result.ok is case["expect"]["ok"], result.speech
    if "result" in case["expect"]:
        assert result.data["result"] == pytest.approx(case["expect"]["result"])
