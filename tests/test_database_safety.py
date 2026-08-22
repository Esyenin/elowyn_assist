import pytest

from elowyn.support.database_safety import (
    UnsafeDatabaseTargetError,
    assert_same_database_server,
    assert_test_database_url,
)


@pytest.mark.parametrize(
    "url",
    [
        "postgresql+asyncpg://owner@localhost/elowyn",
        "postgresql+asyncpg://owner@localhost/postgres",
        "sqlite:///elowyn_test.db",
    ],
)
def test_database_guard_rejects_production_or_non_postgresql_targets(url: str) -> None:
    with pytest.raises(UnsafeDatabaseTargetError):
        assert_test_database_url(url)


def test_destructive_guard_requires_recovery_prefix() -> None:
    with pytest.raises(UnsafeDatabaseTargetError):
        assert_test_database_url(
            "postgresql+asyncpg://owner@localhost/elowyn_test", destructive=True
        )
    assert_test_database_url(
        "postgresql+asyncpg://owner@localhost/elowyn_recovery_test", destructive=True
    )


def test_admin_and_target_must_use_same_server() -> None:
    with pytest.raises(UnsafeDatabaseTargetError):
        assert_same_database_server(
            "postgresql+asyncpg://admin@production.example/postgres",
            "postgresql+asyncpg://owner@localhost/elowyn_recovery_test",
        )
