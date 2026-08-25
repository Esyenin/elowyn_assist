from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


def _launcher_module():
    path = Path(__file__).parents[1] / "build" / "start_elowyn_personal.py"
    spec = importlib.util.spec_from_file_location("elowyn_personal_launcher", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _config() -> dict[str, str]:
    return {
        "DATABASE_URL": (
            "postgresql+asyncpg:"
            "//elowyn_personal_runtime:synthetic@127.0.0.1:5432/"
            "elowyn_personal"
        ),
        "TELEGRAM_BOT_TOKEN": "telegram-secret",
        "TELEGRAM_ALLOWED_USER_ID": "123",
        "NVIDIA_API_KEY": "nvidia-secret",
        "NVIDIA_MODEL": "synthetic-model",
        "HINDSIGHT_API_URL": "http://127.0.0.1:8888",
        "HINDSIGHT_BANK_ID": "elowyn-personal-v030",
    }


def test_hindsight_process_receives_no_core_or_telegram_credentials(monkeypatch) -> None:
    launcher = _launcher_module()
    monkeypatch.setenv("DATABASE_URL", "development-secret")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "inherited-telegram-secret")
    monkeypatch.setenv("ELOWYN_ADMIN_DATABASE_URL", "owner-secret")
    monkeypatch.setenv("TEST_DATABASE_URL", "test-secret")

    environment = launcher._hindsight_environment(_config())

    assert "DATABASE_URL" not in environment
    assert "TELEGRAM_BOT_TOKEN" not in environment
    assert "ELOWYN_ADMIN_DATABASE_URL" not in environment
    assert "TEST_DATABASE_URL" not in environment
    assert environment["HINDSIGHT_API_DATABASE_URL"] == "pg0://elowyn-personal-v030"
    assert environment["HINDSIGHT_API_LLM_API_KEY"] == "nvidia-secret"


def test_personal_launcher_rejects_development_database() -> None:
    launcher = _launcher_module()
    config = _config()
    config["DATABASE_URL"] = (
        "postgresql+asyncpg:"
        "//elowyn_dev_runtime:synthetic@127.0.0.1:5432/elowyn_dev"
    )

    with pytest.raises(RuntimeError, match="non-personal Core database"):
        launcher._validate_isolation(config)
