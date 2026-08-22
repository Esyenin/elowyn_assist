"""Runtime-only model provider configuration."""

from __future__ import annotations

import os
from typing import Any, cast

from dotenv import load_dotenv
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.openai import OpenAIProvider

NVIDIA_BASE_URL = "https://integrate.api.nvidia.com/v1"


def _required_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"{name} must be configured")
    return value


def build_runtime_model() -> OpenAIChatModel:
    """Build the configured hosted model without exposing provider details to Core."""

    load_dotenv(override=False)
    provider = OpenAIProvider(
        base_url=NVIDIA_BASE_URL,
        api_key=_required_env("NVIDIA_API_KEY"),
    )
    model_name = cast(Any, _required_env("NVIDIA_MODEL"))
    return OpenAIChatModel(model_name, provider=provider)
