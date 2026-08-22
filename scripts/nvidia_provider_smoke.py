"""Minimal real-provider smoke using synthetic prompts only."""

from __future__ import annotations

import asyncio

from pydantic_ai import Agent

from elowyn.domain.commands import TaskCreate
from elowyn.provider import build_runtime_model


async def smoke() -> None:
    model = build_runtime_model()

    text_agent = Agent(model, system_prompt="Follow the synthetic smoke instruction exactly.")
    text_result = await text_agent.run(
        "Synthetic provider smoke. Reply with one short plain-text sentence containing SMOKE_OK."
    )
    if "SMOKE_OK" not in str(text_result.output):
        raise RuntimeError("provider returned an unexpected text response")

    captured: list[TaskCreate] = []
    tool_agent = Agent(
        model,
        system_prompt=(
            "For this synthetic smoke, call create_task exactly once, then confirm briefly."
        ),
    )

    @tool_agent.tool_plain(sequential=True)
    async def create_task(command: TaskCreate) -> str:
        """Capture a validated synthetic Task command."""
        captured.append(command)
        return "synthetic task accepted"

    await tool_agent.run(
        "Create the synthetic task 'NVIDIA tool smoke' with description 'synthetic only'."
    )
    if len(captured) != 1:
        raise RuntimeError("provider did not call the domain-shaped tool exactly once")
    if captured[0].title != "NVIDIA tool smoke" or captured[0].description != "synthetic only":
        raise RuntimeError("provider supplied incorrect validated JSON arguments")

    print("NVIDIA text smoke passed")
    print("NVIDIA tool/JSON smoke passed")


async def main() -> int:
    try:
        await smoke()
    except Exception as exc:
        print(f"NVIDIA smoke failed safely: {type(exc).__name__}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
