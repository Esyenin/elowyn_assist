from pathlib import Path


_PROMPT_PATH = Path(__file__).resolve().parent.parent / "prompts" / "identity_v0_1.md"


def load_identity_prompt() -> str:
    return _PROMPT_PATH.read_text(encoding="utf-8")
