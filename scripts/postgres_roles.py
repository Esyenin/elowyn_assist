"""Provision owner/runtime roles and apply least-privilege grants.

The command never prints connection URLs or passwords. Use ``--test`` for acceptance databases;
it refuses database names without an explicit test marker.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import re

import asyncpg
from sqlalchemy.engine import make_url

from elowyn.support.database_safety import assert_same_database_server, assert_test_database_url

IDENTIFIER = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")
HISTORY_TABLES = ("sources", "operations", "events")
INSERT_ONLY_TABLES = (
    "conversations",
    "messages",
    "source_dependencies",
    "decision_alternatives",
    "task_goal_links",
    "project_goal_links",
    "task_dependencies",
    "entity_relations",
)
MUTABLE_COLUMNS = {
    "entities": ("superseded_by_entity_id", "updated_at"),
    "tasks": (
        "title",
        "description",
        "status",
        "importance",
        "importance_source_id",
        "deadline_at",
        "deadline_type",
        "estimated_duration_minutes",
        "estimate_source_id",
        "parent_task_id",
        "primary_project_id",
        "auto_complete_from_children",
        "completed_at",
    ),
    "projects": (
        "name",
        "description",
        "status",
        "importance",
        "importance_source_id",
        "target_date",
        "target_date_type",
        "parent_project_id",
        "current_summary",
        "current_summary_updated_at",
        "completed_at",
    ),
    "goals": (
        "title",
        "description",
        "status",
        "importance",
        "importance_source_id",
        "target_date",
        "target_date_type",
        "parent_goal_id",
        "achieved_at",
    ),
    "success_criteria": (
        "description",
        "status",
        "confidence",
        "evaluation_summary",
        "evaluation_source_id",
        "updated_at",
    ),
    "decisions": ("status",),
}


def identifier(value: str) -> str:
    if not IDENTIFIER.fullmatch(value):
        raise ValueError(f"unsafe PostgreSQL identifier: {value!r}")
    return f'"{value}"'


def literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def asyncpg_url(url: str) -> str:
    return url.replace("postgresql+asyncpg://", "postgresql://", 1)


def validate_configuration(args: argparse.Namespace, *, require_admin: bool) -> None:
    target = make_url(args.target_url)
    if target.database != args.database:
        raise ValueError("--database must exactly match the target URL database")
    if args.owner_role == args.runtime_role:
        raise ValueError("owner and runtime roles must be distinct")
    for role in (args.owner_role, args.runtime_role):
        if role in {"postgres", "public"} or role.startswith("pg_"):
            raise ValueError(f"protected PostgreSQL role cannot be managed: {role}")
        identifier(role)
    if require_admin:
        assert_same_database_server(args.admin_url, args.target_url)
    elif target.username != args.owner_role:
        raise ValueError("runtime grants must be applied through the configured owner role")


async def provision(args: argparse.Namespace) -> None:
    validate_configuration(args, require_admin=True)
    if args.test:
        assert_test_database_url(args.target_url)
    owner = identifier(args.owner_role)
    runtime = identifier(args.runtime_role)
    database = identifier(args.database)
    owner_password = literal(os.environ["ELOWYN_OWNER_PASSWORD"])
    runtime_password = literal(os.environ["ELOWYN_RUNTIME_PASSWORD"])

    connection = await asyncpg.connect(asyncpg_url(args.admin_url))
    try:
        await connection.execute(
            f"""
            DO $$ BEGIN
              IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = {literal(args.owner_role)}) THEN
                CREATE ROLE {owner} LOGIN;
              END IF;
              IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = {literal(args.runtime_role)}) THEN
                CREATE ROLE {runtime} LOGIN;
              END IF;
            END $$;
            ALTER ROLE {owner} WITH NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION
              NOBYPASSRLS NOINHERIT
              PASSWORD {owner_password};
            ALTER ROLE {runtime} WITH NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION
              NOBYPASSRLS NOINHERIT
              PASSWORD {runtime_password};
            ALTER DATABASE {database} OWNER TO {owner};
            REVOKE ALL ON DATABASE {database} FROM {runtime};
            REVOKE TEMPORARY ON DATABASE {database} FROM PUBLIC;
            GRANT CONNECT ON DATABASE {database} TO {runtime};
            """
        )
        for role_name in (args.owner_role, args.runtime_role):
            memberships = await connection.fetch(
                "SELECT parent.rolname "
                "FROM pg_auth_members membership "
                "JOIN pg_roles member ON member.oid = membership.member "
                "JOIN pg_roles parent ON parent.oid = membership.roleid "
                "WHERE member.rolname = $1 ORDER BY parent.rolname",
                role_name,
            )
            for membership in memberships:
                await connection.execute(
                    f"REVOKE {identifier(membership['rolname'])} FROM {identifier(role_name)}"
                )
    finally:
        await connection.close()

    target = await asyncpg.connect(asyncpg_url(args.target_url))
    try:
        await target.execute(
            f"""
            ALTER SCHEMA public OWNER TO {owner};
            REVOKE CREATE ON SCHEMA public FROM PUBLIC;
            REVOKE ALL ON SCHEMA public FROM {runtime};
            GRANT USAGE ON SCHEMA public TO {runtime};
            """
        )
        tables = await target.fetch(
            "SELECT tablename FROM pg_tables WHERE schemaname = 'public' ORDER BY tablename"
        )
        for row in tables:
            await target.execute(
                f"ALTER TABLE public.{identifier(row['tablename'])} OWNER TO {owner}"
            )
        sequences = await target.fetch(
            "SELECT sequencename FROM pg_sequences WHERE schemaname = 'public' "
            "ORDER BY sequencename"
        )
        for row in sequences:
            await target.execute(
                f"ALTER SEQUENCE public.{identifier(row['sequencename'])} OWNER TO {owner}"
            )
    finally:
        await target.close()


async def grant_runtime(args: argparse.Namespace) -> None:
    validate_configuration(args, require_admin=False)
    if args.test:
        assert_test_database_url(args.target_url)
    runtime = identifier(args.runtime_role)
    connection = await asyncpg.connect(asyncpg_url(args.target_url))
    try:
        await connection.execute(
            f"REVOKE ALL PRIVILEGES ON ALL TABLES IN SCHEMA public FROM {runtime}"
        )
        tables = (*HISTORY_TABLES, *INSERT_ONLY_TABLES, *MUTABLE_COLUMNS)
        for table in tables:
            await connection.execute(f"GRANT SELECT, INSERT ON {identifier(table)} TO {runtime}")
        for table, columns in MUTABLE_COLUMNS.items():
            column_list = ", ".join(identifier(column) for column in columns)
            await connection.execute(
                f"GRANT UPDATE ({column_list}) ON {identifier(table)} TO {runtime}"
            )
        await connection.execute(
            f"""
            REVOKE CREATE ON SCHEMA public FROM PUBLIC;
            REVOKE CREATE ON SCHEMA public FROM {runtime};
            REVOKE ALL PRIVILEGES ON alembic_version FROM {runtime};
            """
        )
    finally:
        await connection.close()


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser()
    result.add_argument("command", choices=("provision", "grant-runtime"))
    result.add_argument("--admin-url", default=os.environ.get("ELOWYN_ADMIN_DATABASE_URL"))
    result.add_argument("--target-url", required=True)
    result.add_argument("--database", required=True)
    result.add_argument("--owner-role", default="elowyn_owner")
    result.add_argument("--runtime-role", default="elowyn_runtime")
    result.add_argument("--test", action="store_true")
    return result


async def main() -> None:
    args = parser().parse_args()
    if args.command == "provision":
        if not args.admin_url:
            raise ValueError("--admin-url or ELOWYN_ADMIN_DATABASE_URL is required")
        await provision(args)
    else:
        await grant_runtime(args)


if __name__ == "__main__":
    asyncio.run(main())
