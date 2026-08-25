"""Safe classification of external model-provider failures."""

from __future__ import annotations

from dataclasses import dataclass

import httpx
from pydantic_ai.exceptions import ModelAPIError, ModelHTTPError

TRANSIENT_MODEL_STATUS_CODES = frozenset({500, 502, 503, 504})


@dataclass(frozen=True)
class TransientModelError:
    """Non-sensitive fields safe to include in an operational log."""

    error_type: str
    model_name: str | None
    status_code: int | None


def classify_transient_model_error(error: BaseException) -> TransientModelError | None:
    """Return safe diagnostics only for retryable external model failures.

    Provider response bodies are deliberately excluded because they may contain
    request fragments. Authentication/configuration 4xx errors are not masked.
    """

    if isinstance(error, ModelHTTPError):
        if error.status_code not in TRANSIENT_MODEL_STATUS_CODES:
            return None
        return TransientModelError(
            error_type=type(error).__name__,
            model_name=error.model_name,
            status_code=error.status_code,
        )
    if isinstance(error, ModelAPIError) and _caused_by_timeout(error):
        return TransientModelError(
            error_type=type(error).__name__,
            model_name=error.model_name,
            status_code=None,
        )
    return None


def _caused_by_timeout(error: BaseException) -> bool:
    current: BaseException | None = error
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, httpx.TimeoutException):
            return True
        # OpenAI-compatible providers wrap their timeout class in ModelAPIError.
        if type(current).__name__ == "APITimeoutError" and type(current).__module__.startswith(
            "openai"
        ):
            return True
        current = current.__cause__ or current.__context__
    return False
