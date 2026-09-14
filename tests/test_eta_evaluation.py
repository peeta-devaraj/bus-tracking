"""The evaluation's conclusions, pinned as tests.

docs/eta-evaluation.md makes three claims. These tests re-derive each one on a
smaller simulation (8 training days, 3 test days, one route), so a change to
the model, the simulator or the evaluation that quietly breaks a claim fails
here rather than leaving a stale report in the repo.

Thresholds sit well inside what three different seeds produced when this was
written (structure gain 45-48% on structured traffic, 0 to -1% on the control,
stop-time gain 57-63% on the control). They are meant to catch a broken
method, not to chase exact numbers.

Everything here is simulated traffic; see tools/traffic.py.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools"))

from eval_eta import evaluate, summarise  # noqa: E402
from traffic import STRUCTURED, UNIFORM  # noqa: E402

ROUTE = "NGL-VAD-KKD"


@pytest.fixture(scope="module")
def structured():
    return summarise(evaluate(ROUTE, STRUCTURED, train_days=8, test_days=3, seed=1)["graded"])


@pytest.fixture(scope="module")
def uniform():
    return summarise(evaluate(ROUTE, UNIFORM, train_days=8, test_days=3, seed=1)["graded"])


def test_there_is_enough_evidence_to_conclude_anything(structured, uniform):
    assert structured["n"] > 5000
    assert uniform["n"] > 5000


def test_learning_where_and_when_helps_when_that_structure_exists(structured):
    """Claim 1: beyond an average pace, place-and-time pace cuts error."""
    assert structured["structure_gain"] > 0.25


def test_the_control_shows_no_gain_from_structure_that_is_not_there(uniform):
    """Claim 2: on traffic with no place or time structure, the learned model
    must not beat an average pace. If it does, the evaluation is leaking
    information -- this is the check that caught the original confound."""
    assert abs(uniform["structure_gain"]) < 0.08


def test_the_live_estimator_ignores_time_spent_stopped(uniform):
    """Claim 3: even with no structure at all, accounting for stop time beats
    dividing by the bus's moving speed. This is a flaw in the live estimator,
    independent of learning anything about Nagercoil."""
    assert uniform["pace_gain"] > 0.30


def test_the_learned_model_is_never_worse_than_what_it_replaces(structured, uniform):
    for result in (structured, uniform):
        assert result["learned_mae"] < result["naive_mae"]
