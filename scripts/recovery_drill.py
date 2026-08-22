"""Destructive dump/restore drill restricted to an explicitly named recovery test database."""

from __future__ import annotations

import argparse
import asyncio
import os
import subprocess
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import asyncpg
from sqlalchemy import select
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from elowyn.db.models import Event, SuccessCriterion, Task
from elowyn.domain.commands import (
    DecisionAlternativeCreate,
    DecisionCreate,
    EntityRelationCreate,
    GoalCreate,
    ProjectCreate,
    SuccessCriterionAssessment,
    SuccessCriterionCreate,
    TaskAssessment,
    TaskCreate,
    TaskDependencyCreate,
    TaskUpdate,
)
from elowyn.domain.enums import ActorType, RelationType, SuccessCriterionStatus, TransportType
from elowyn.domain.messages import IncomingMessage
from elowyn.services.conversation import ConversationService
from elowyn.services.world_state import ActionContext, WorldStateService
from elowyn.support.consistency import ConsistencyVerifier
from elowyn.support.database_safety import assert_same_database_server, assert_test_database_url
from elowyn.support.digest import state_history_digest

RECOVERY_DATABASE_SENTINEL = "elowyn-recovery-test-only"


def asyncpg_url(url: str) -> str:
    return url.replace("postgresql+asyncpg://", "postgresql://", 1)


def quote_identifier(value: str) -> str:
    if not value.replace("_", "").isalnum() or not value[0].isalpha():
        raise ValueError("unsafe PostgreSQL identifier")
    return f'"{value}"'


async def recreate_database(admin_url: str, target_url: str) -> None:
    assert_test_database_url(target_url, destructive=True)
    assert_same_database_server(admin_url, target_url)
    parsed = make_url(target_url)
    database = parsed.database or ""
    owner = parsed.username or ""
    connection = await asyncpg.connect(asyncpg_url(admin_url))
    try:
        existing = await connection.fetchrow(
            "SELECT shobj_description(oid, 'pg_database') AS comment "
            "FROM pg_database WHERE datname = $1",
            database,
        )
        if existing is not None and existing["comment"] != RECOVERY_DATABASE_SENTINEL:
            raise RuntimeError(
                "refusing to replace an existing database without the recovery-test sentinel"
            )
        await connection.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
            "WHERE datname = $1 AND pid <> pg_backend_pid()",
            database,
        )
        await connection.execute(f"DROP DATABASE IF EXISTS {quote_identifier(database)}")
        await connection.execute(
            f"CREATE DATABASE {quote_identifier(database)} OWNER {quote_identifier(owner)}"
        )
        await connection.execute(
            f"COMMENT ON DATABASE {quote_identifier(database)} IS '{RECOVERY_DATABASE_SENTINEL}'"
        )
    finally:
        await connection.close()


def pg_environment(url: str) -> tuple[dict[str, str], list[str]]:
    parsed = make_url(url)
    environment = dict(os.environ)
    if parsed.password:
        environment["PGPASSWORD"] = parsed.password
    args = [
        "--host",
        parsed.host or "localhost",
        "--port",
        str(parsed.port or 5432),
        "--username",
        parsed.username or "",
        "--dbname",
        parsed.database or "",
    ]
    return environment, args


def pg_tool(pg_bin: Path | None, name: str) -> str:
    executable = f"{name}.exe" if os.name == "nt" else name
    return str(pg_bin / executable) if pg_bin else executable


def run_checked(command: list[str], *, environment: dict[str, str]) -> None:
    result = subprocess.run(command, env=environment, text=True, capture_output=True, check=False)
    if result.returncode:
        message = result.stderr.strip().splitlines()[-1] if result.stderr.strip() else "failed"
        raise RuntimeError(f"{Path(command[0]).name} failed: {message}")


async def seed_database(url: str) -> UUID:
    engine = create_async_engine(url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session:
            conversation = ConversationService(session)

            async def user_source(message_id: str, content: str):
                turn = await conversation.ingest_user_message(
                    IncomingMessage(
                        transport=TransportType.TELEGRAM,
                        external_conversation_id="recovery-drill",
                        external_message_id=message_id,
                        text=content,
                        sent_at=datetime.now(UTC),
                    )
                )
                return turn.source

            user = await user_source("seed-1", "Создай realistic recovery state")
            correction = await user_source("seed-2", "Исправь название задачи")
            undo_source = await user_source("seed-3", "Отмени последнее исправление")
            service = WorldStateService(session)
            ctx = ActionContext(ActorType.USER, user, description="recovery realistic seed")
            project = await service.create_project(ProjectCreate(name="Recovery project"), ctx)
            goal = await service.create_goal(
                GoalCreate(
                    title="Recovery goal",
                    success_criteria=[SuccessCriterionCreate(description="Digest matches")],
                ),
                ctx,
            )
            prerequisite = await service.create_task(TaskCreate(title="Backup"), ctx)
            task = await service.create_task(
                TaskCreate(
                    title="Restore",
                    primary_project_id=project.entity_id,
                    goal_ids=[goal.entity_id],
                ),
                ctx,
            )
            await service.add_task_dependency(
                TaskDependencyCreate(
                    prerequisite_task_id=prerequisite.entity_id,
                    dependent_task_id=task.entity_id,
                ),
                ctx,
            )
            await service.create_relation(
                EntityRelationCreate(
                    source_entity_id=project.entity_id,
                    target_entity_id=goal.entity_id,
                    relation_type=RelationType.SUPPORTS,
                ),
                ctx,
            )
            old_decision = await service.create_decision(
                DecisionCreate(
                    title="Backup format",
                    chosen_option="custom",
                    alternatives=[DecisionAlternativeCreate(option_text="plain")],
                ),
                ctx,
            )
            await service.create_decision(
                DecisionCreate(
                    title="Backup format",
                    chosen_option="custom compressed",
                    supersedes_decision_id=old_decision.entity_id,
                ),
                ctx,
            )
            await service.assess_task(
                TaskAssessment(
                    entity_id=task.entity_id,
                    importance=5,
                    estimated_duration_minutes=30,
                    confidence=0.9,
                    reason_summary="Recovery is acceptance-critical",
                ),
                evidence_source=user,
            )
            criterion = (
                await session.execute(
                    select(SuccessCriterion).where(SuccessCriterion.goal_id == goal.entity_id)
                )
            ).scalar_one()
            await service.assess_success_criterion(
                SuccessCriterionAssessment(
                    criterion_id=criterion.id,
                    status=SuccessCriterionStatus.MET,
                    confidence=0.8,
                    evaluation_summary="Seed created",
                    reason_summary="All seed actions completed",
                ),
                evidence_source=user,
            )
            await service.update_task(
                TaskUpdate(entity_id=task.entity_id, title="Restore corrected"),
                ActionContext(ActorType.USER, correction, description="correction"),
            )
            await service.undo_last_change(
                ActionContext(ActorType.USER, undo_source, description="undo correction"),
                entity_id=task.entity_id,
            )
            await session.commit()
            return task.entity_id
    finally:
        await engine.dispose()


async def digest(url: str) -> str:
    engine = create_async_engine(url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session:
            return await state_history_digest(session)
    finally:
        await engine.dispose()


async def verify_consistency(url: str) -> None:
    engine = create_async_engine(url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session:
            (await ConsistencyVerifier(session).verify()).require_ok()
    finally:
        await engine.dispose()


async def post_restore_mutations(url: str, task_id: UUID) -> None:
    engine = create_async_engine(url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session:
            conversation = ConversationService(session)

            async def user_source(message_id: str, content: str):
                turn = await conversation.ingest_user_message(
                    IncomingMessage(
                        transport=TransportType.TELEGRAM,
                        external_conversation_id="recovery-drill",
                        external_message_id=message_id,
                        text=content,
                        sent_at=datetime.now(UTC),
                    )
                )
                return turn.source

            update_source = await user_source("restore-1", "Обнови после восстановления")
            correction_source = await user_source("restore-2", "Исправь обновление")
            undo_source = await user_source("restore-3", "Отмени исправление")
            service = WorldStateService(session)
            await service.update_task(
                TaskUpdate(entity_id=task_id, title="After restore"),
                ActionContext(ActorType.USER, update_source),
            )
            await service.update_task(
                TaskUpdate(entity_id=task_id, title="After restore correction"),
                ActionContext(ActorType.USER, correction_source),
            )
            undo = await service.undo_last_change(
                ActionContext(ActorType.USER, undo_source), entity_id=task_id
            )
            await session.commit()
            task = await session.get(Task, task_id)
            persisted_undo = await session.get(Event, undo.id)
            if task is None or task.title != "After restore" or persisted_undo is None:
                raise RuntimeError("post-restore update/correction/undo verification failed")
    finally:
        await engine.dispose()


async def drill(args: argparse.Namespace) -> None:
    if os.environ.get("ELOWYN_ALLOW_DESTRUCTIVE_TEST_DB") != "YES":
        raise RuntimeError("set ELOWYN_ALLOW_DESTRUCTIVE_TEST_DB=YES for the recovery drill")
    assert_test_database_url(args.target_url, destructive=True)
    pg_bin = Path(args.pg_bin).resolve() if args.pg_bin else None
    await recreate_database(args.admin_url, args.target_url)
    migration_env = dict(os.environ, DATABASE_URL=args.target_url)
    run_checked(
        [sys.executable, "-m", "alembic", "upgrade", "0001_initial"],
        environment=migration_env,
    )
    task_id = await seed_database(args.target_url)
    run_checked([sys.executable, "-m", "alembic", "upgrade", "head"], environment=migration_env)
    await verify_consistency(args.target_url)
    before = await digest(args.target_url)

    with tempfile.TemporaryDirectory(prefix="elowyn-recovery-") as directory:
        dump_path = Path(directory) / "elowyn.dump"
        pg_env, connection_args = pg_environment(args.target_url)
        run_checked(
            [
                pg_tool(pg_bin, "pg_dump"),
                "--format=custom",
                "--file",
                str(dump_path),
                *connection_args,
            ],
            environment=pg_env,
        )
        await recreate_database(args.admin_url, args.target_url)
        run_checked(
            [
                pg_tool(pg_bin, "pg_restore"),
                "--exit-on-error",
                "--no-owner",
                *connection_args,
                str(dump_path),
            ],
            environment=pg_env,
        )

    after = await digest(args.target_url)
    if before != after:
        raise RuntimeError(f"digest mismatch: before={before}, after={after}")
    await verify_consistency(args.target_url)
    await post_restore_mutations(args.target_url, task_id)
    await verify_consistency(args.target_url)
    print(f"recovery drill passed; digest={before}")


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser()
    result.add_argument("--admin-url", required=True)
    result.add_argument("--target-url", required=True)
    result.add_argument("--pg-bin")
    return result


if __name__ == "__main__":
    asyncio.run(drill(parser().parse_args()))
