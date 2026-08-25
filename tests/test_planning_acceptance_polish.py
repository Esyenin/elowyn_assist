from elowyn.assistant.planning_resolution import (
    historical_candidate_duration_days,
    is_collaborative_first_item_request,
    is_collaborative_next_item_request,
    is_compact_plan_request,
    is_historical_rejected_candidate_question,
    is_presence_small_talk,
)
from elowyn.assistant.planning_tools import planning_policy


def test_compact_plan_request_is_rendering_intent_not_revision() -> None:
    assert is_compact_plan_request(
        "Можешь этот же план написать короче? Очень длинно получилось"
    )
    assert is_compact_plan_request("Покажи план кратко, TL;DR")
    assert not is_compact_plan_request("Сократи план до пяти дней")


def test_work_together_intent_is_not_inferred_from_progress_or_approval_language() -> None:
    assert is_collaborative_first_item_request("Сделай пока первый пункт вместе со мной")
    assert is_collaborative_next_item_request("Сделай следующий пункт вместе со мной")
    assert not is_collaborative_first_item_request("Первый пункт уже сделан")
    assert not is_collaborative_first_item_request("Да, утверждаю первый вариант")


def test_rejected_history_and_presence_are_distinct_narrow_intents() -> None:
    assert is_historical_rejected_candidate_question(
        "Что стало с предыдущим отклонённым вариантом на 5 дней?"
    )
    assert is_presence_small_talk("Ты тут?")
    assert not is_presence_small_talk("Ты тут? Что с нашим планом?")
    assert historical_candidate_duration_days(
        "Что стало с предыдущим отклонённым вариантом на 5 дней?"
    ) == 5


def test_policy_requires_canonical_history_and_compact_status_answers() -> None:
    policy = planning_policy()

    assert "только по read_current_plan/read_plan_history и canonical Events" in policy
    assert "не объясняй её по Conversation/Memory" in policy
    assert "status, next action, approval/reject confirmation" in policy
    assert "никогда не\n  дублируй один Plan дважды" in policy
