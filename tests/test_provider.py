from __future__ import annotations

import pytest
from pydantic_ai.models.openai import OpenAIChatModel

from elowyn.provider import NVIDIA_BASE_URL, build_runtime_model


def test_nvidia_runtime_model_requires_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NVIDIA_MODEL", "synthetic/model")
    monkeypatch.delenv("NVIDIA_API_KEY", raising=False)
    monkeypatch.setattr("elowyn.provider.load_dotenv", lambda **_: False)

    with pytest.raises(RuntimeError, match="NVIDIA_API_KEY must be configured"):
        build_runtime_model()


def test_nvidia_runtime_model_uses_runtime_configuration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("NVIDIA_MODEL", "synthetic/model")
    monkeypatch.setenv("NVIDIA_API_KEY", "synthetic-provider-key")
    monkeypatch.setattr("elowyn.provider.load_dotenv", lambda **_: False)

    model = build_runtime_model()

    assert isinstance(model, OpenAIChatModel)
    assert model.model_name == "synthetic/model"
    assert model._provider.base_url.rstrip("/") == NVIDIA_BASE_URL
