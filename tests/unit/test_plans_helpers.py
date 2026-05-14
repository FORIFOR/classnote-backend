"""Unit tests for the centralised plan-ladder helpers (PR1).

Spec: ``app/services/plans.py`` — `has_plan_at_least` /
`choose_higher_plan` / `plan_rank` are the single source of truth for
plan-gate comparisons after the business-license tier was introduced.
"""

from __future__ import annotations

import pytest

from app.services.plans import (
    PLAN_RANK,
    choose_higher_plan,
    has_plan_at_least,
    max_plan,
    plan_rank,
)


# ── plan_rank ────────────────────────────────────────────────────────


def test_plan_rank_known_tiers_strictly_increasing() -> None:
    """The full ladder is free < basic < standard < business. New tiers
    must slot in here so downstream gates stay monotonic."""
    assert plan_rank("free") < plan_rank("basic")
    assert plan_rank("basic") < plan_rank("standard")
    assert plan_rank("standard") < plan_rank("business")


def test_plan_rank_unknown_falls_back_to_free() -> None:
    assert plan_rank(None) == PLAN_RANK["free"]
    assert plan_rank("") == PLAN_RANK["free"]
    assert plan_rank("pro") == PLAN_RANK["free"]  # not in ladder yet
    assert plan_rank("nonsense") == PLAN_RANK["free"]


# ── has_plan_at_least ────────────────────────────────────────────────


@pytest.mark.parametrize(
    "current,required,expected",
    [
        # business covers everything below it.
        ("business", "business", True),
        ("business", "standard", True),
        ("business", "basic", True),
        ("business", "free", True),
        # standard does not satisfy business.
        ("standard", "business", False),
        ("standard", "standard", True),
        ("standard", "basic", True),
        # basic.
        ("basic", "business", False),
        ("basic", "standard", False),
        ("basic", "basic", True),
        # free.
        ("free", "basic", False),
        ("free", "free", True),
        # None / unknown current.
        (None, "basic", False),
        (None, "free", True),
        ("pro", "basic", False),
    ],
)
def test_has_plan_at_least(current: str | None, required: str, expected: bool) -> None:
    assert has_plan_at_least(current, required) is expected


# ── choose_higher_plan ───────────────────────────────────────────────


def test_choose_higher_plan_picks_higher() -> None:
    assert choose_higher_plan("basic", "business") == "business"
    assert choose_higher_plan("business", "basic") == "business"
    assert choose_higher_plan("standard", "basic") == "standard"
    assert choose_higher_plan("free", "basic") == "basic"


def test_choose_higher_plan_ties_to_first_argument() -> None:
    # When ranks are equal, `a` wins. Documented behaviour so callers
    # can rely on it (e.g. "prefer the Apple subscription on tie").
    assert choose_higher_plan("basic", "basic") == "basic"
    assert choose_higher_plan("business", "business") == "business"


def test_choose_higher_plan_normalises_none() -> None:
    assert choose_higher_plan(None, "business") == "business"
    assert choose_higher_plan("business", None) == "business"
    assert choose_higher_plan(None, None) == "free"


# ── max_plan still consistent with the extended ladder ───────────────


def test_max_plan_recognises_business() -> None:
    """`max_plan` was the only public helper before PR1. After the
    ladder gained `standard` and `business`, it must keep picking the
    highest correctly — regression guard."""
    assert max_plan(["free", "basic", "business"]) == "business"
    assert max_plan(["standard", "basic"]) == "standard"
    assert max_plan(["business", "standard"]) == "business"
    assert max_plan([]) == "free"
    assert max_plan(["nonsense", "pro"]) == "free"
