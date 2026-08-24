from uuid import uuid4

import pytest
from pydantic import ValidationError

from elowyn.assistant.planning_tools import CreatePlanWithCandidateProposal, planning_policy
from elowyn.domain.enums import PlanVersionBasisRole
from elowyn.domain.planning_commands import (
    PlanCandidateCreate,
    PlanVersionBasisCreate,
    PlanVersionItemCreate,
    PlanVersionItemDependencyCreate,
)


@pytest.mark.parametrize("field", ["summary", "proposed_strategy_snapshot"])
def test_candidate_rejects_blank_required_text(field: str) -> None:
    values = {
        "plan_id": uuid4(),
        "summary": "summary",
        "proposed_strategy_snapshot": "strategy",
    }
    values[field] = "   "
    with pytest.raises(ValidationError):
        PlanCandidateCreate(**values)


def test_items_reject_invalid_ordinal_duration_and_duplicate_identity() -> None:
    with pytest.raises(ValidationError):
        PlanVersionItemCreate(ordinal=0, title="bad")
    with pytest.raises(ValidationError):
        PlanVersionItemCreate(ordinal=1, title="bad", estimated_duration_minutes=0)
    item_id = uuid4()
    with pytest.raises(ValidationError, match="identifiers"):
        PlanCandidateCreate(
            plan_id=uuid4(),
            summary="summary",
            proposed_strategy_snapshot="strategy",
            items=[
                PlanVersionItemCreate(id=item_id, ordinal=1, title="one"),
                PlanVersionItemCreate(id=item_id, ordinal=2, title="two"),
            ],
        )


def test_candidate_rejects_invalid_or_duplicate_dependency_references() -> None:
    first = PlanVersionItemCreate(ordinal=1, title="one")
    second = PlanVersionItemCreate(ordinal=2, title="two")
    unknown = uuid4()
    with pytest.raises(ValidationError, match="unknown item"):
        PlanCandidateCreate(
            plan_id=uuid4(),
            summary="summary",
            proposed_strategy_snapshot="strategy",
            items=[first, second],
            dependencies=[
                PlanVersionItemDependencyCreate(
                    prerequisite_item_id=first.id,
                    dependent_item_id=unknown,
                )
            ],
        )
    edge = PlanVersionItemDependencyCreate(
        prerequisite_item_id=first.id,
        dependent_item_id=second.id,
    )
    with pytest.raises(ValidationError, match="unique"):
        PlanCandidateCreate(
            plan_id=uuid4(),
            summary="summary",
            proposed_strategy_snapshot="strategy",
            items=[first, second],
            dependencies=[edge, edge],
        )


def test_candidate_rejects_duplicate_basis() -> None:
    basis = PlanVersionBasisCreate(
        entity_id=uuid4(),
        event_id=uuid4(),
        role=PlanVersionBasisRole.GOAL,
    )
    with pytest.raises(ValidationError, match="basis"):
        PlanCandidateCreate(
            plan_id=uuid4(),
            summary="summary",
            proposed_strategy_snapshot="strategy",
            basis=[basis, basis],
        )


def test_create_plan_tool_validates_provider_stringified_nested_objects() -> None:
    proposal = CreatePlanWithCandidateProposal.model_validate(
        {
            "plan": '{"title":"Synthetic plan"}',
            "candidate": (
                '{"summary":"Synthetic candidate",'
                '"proposed_strategy_snapshot":"Validated strategy",'
                '"items":[{"ordinal":1,"title":"Validated item"}]}'
            ),
        }
    )
    assert proposal.plan.title == "Synthetic plan"
    assert proposal.candidate.items[0].title == "Validated item"

    with pytest.raises(ValidationError):
        CreatePlanWithCandidateProposal.model_validate(
            {
                "plan": '"not an object"',
                "candidate": '{"summary":"x","proposed_strategy_snapshot":"y"}',
            }
        )


def test_planning_policy_forbids_invented_basis_events() -> None:
    policy = planning_policy()
    assert "Никогда не придумывай event_id" in policy
    assert "оставь basis пустым" in policy
