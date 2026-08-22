from __future__ import annotations

import logging
from uuid import uuid4

from elowyn.assistant.context import build_turn_prompt
from elowyn.db.session import engine


def synthetic_canary() -> str:
    return "synthetic-" + uuid4().hex + "-do-not-leak"


def test_database_engine_hides_sql_bind_parameters() -> None:
    assert engine.sync_engine.hide_parameters is True
    assert not engine.echo


def test_environment_canaries_do_not_enter_prompt_logs_or_exception(monkeypatch, caplog) -> None:
    database_canary = synthetic_canary()
    telegram_canary = synthetic_canary()
    provider_canary = synthetic_canary()
    monkeypatch.setenv("DATABASE_URL", database_canary)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", telegram_canary)
    monkeypatch.setenv("OPENAI_API_KEY", provider_canary)

    with caplog.at_level(logging.DEBUG):
        prompt = build_turn_prompt(user_text="Покажи задачи", world_state="{}", history=[])
        try:
            raise RuntimeError("synthetic provider failure")
        except RuntimeError:
            logging.getLogger("elowyn.test").exception("provider call failed")

    combined = prompt + caplog.text
    assert database_canary not in combined
    assert telegram_canary not in combined
    assert provider_canary not in combined
