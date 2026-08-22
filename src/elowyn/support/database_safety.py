from __future__ import annotations

from sqlalchemy.engine import make_url


class UnsafeDatabaseTargetError(ValueError):
    """Raised when a safety or recovery operation targets a non-test database."""


def assert_test_database_url(url: str, *, destructive: bool = False) -> None:
    """Reject database targets that are not explicitly named as test/ephemeral databases."""

    parsed = make_url(url)
    if not parsed.drivername.startswith("postgresql"):
        raise UnsafeDatabaseTargetError("DB safety tests require PostgreSQL")
    database = (parsed.database or "").lower()
    if not database or not any(marker in database for marker in ("test", "ephemeral")):
        raise UnsafeDatabaseTargetError("database name must contain 'test' or 'ephemeral'")
    if database in {"postgres", "template0", "template1", "elowyn"}:
        raise UnsafeDatabaseTargetError("refusing protected or production-like database")
    if destructive and not database.startswith("elowyn_recovery_"):
        raise UnsafeDatabaseTargetError(
            "destructive recovery targets must start with 'elowyn_recovery_'"
        )


def assert_same_database_server(first_url: str, second_url: str) -> None:
    """Require administrative and target URLs to address the same PostgreSQL server."""

    first = make_url(first_url)
    second = make_url(second_url)
    first_endpoint = ((first.host or "localhost").lower(), first.port or 5432)
    second_endpoint = ((second.host or "localhost").lower(), second.port or 5432)
    if first_endpoint != second_endpoint:
        raise UnsafeDatabaseTargetError("admin and target URLs must use the same host and port")
