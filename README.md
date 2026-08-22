# Elowyn v0.1

Вертикальная первая версия персонального AI-ассистента:

`Telegram → Elowyn → domain tools → validated World State → PostgreSQL → Event/Source → response`

v0.1 сознательно не содержит planner, long-term memory, background workers, desktop
perception и strategic optimizer.

## Архитектурные инварианты

1. Пользователь работает с Elowyn, а не с БД/CRUD.
2. LLM не получает SQL-write доступ; изменения идут через `WorldStateService`.
3. Текущий World State и append-only Event History разделены.
4. `Task`, `Project`, `Goal`, `Decision` используют тонкий общий `Entity` identity.
5. Строгие связи имеют typed tables; дополнительные semantic relations ограничены `RelationType`.
6. `Source` хранит provenance; AI-derived оценки используют `ASSISTANT_INFERENCE` + confidence/evidence.
7. Undo записывает новый inverse Event и не удаляет историю.
8. `Project.current_summary` — только производный cache: любой Domain Event его инвалидирует.
9. Telegram — adapter; Core работает с transport-independent `IncomingMessage`.

## Локальный запуск

Нужны Python 3.12+, PostgreSQL и зависимости проекта.

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e '.[test]'

cp .env.example .env
# Проект не загружает .env магически: экспортируйте переменные в процесс.
set -a
source .env
set +a

alembic upgrade head
python -m elowyn.app
```

Обязательные runtime-переменные:

- `DATABASE_URL` — PostgreSQL через `asyncpg`;
- `TELEGRAM_BOT_TOKEN` — Bot API token;
- `TELEGRAM_ALLOWED_USER_ID` — единственный разрешённый Telegram user id v0.1;
- `NVIDIA_API_KEY` — ключ NVIDIA hosted API;
- `NVIDIA_MODEL` — model id для NVIDIA NIM. Runtime использует OpenAI-compatible endpoint
  `https://integrate.api.nvidia.com/v1`; Core от provider не зависит.

`TELEGRAM_ALLOWED_USER_ID` обязателен: production entrypoint работает deny-by-default, а не открывает
персонального ассистента всем пользователям бота.

## Тесты

Быстрый contract/domain набор не требует PostgreSQL:

```bash
PYTHONPATH=src pytest -q tests/test_schema_contract.py tests/test_world_state_service.py
```

Полный suite:

```bash
pytest -q
```

PostgreSQL acceptance 1–9 требуют отдельную test DB; при заданном URL полный suite запускает и их,
и Pydantic AI conversation eval:

```bash
DATABASE_URL=postgresql+asyncpg://... alembic upgrade head
TEST_DATABASE_URL=postgresql+asyncpg://... pytest -q
```

CI поднимает PostgreSQL service автоматически, проверяет upgrade/downgrade/upgrade миграции, Ruff,
mypy, compile, role permissions, concurrency, consistency stress, secret audit, recovery drill и все
acceptance/eval tests.

## PostgreSQL privilege model

Deployment использует две отдельные роли:

- migration/owner применяет Alembic и владеет schema objects;
- runtime не является owner/superuser, не имеет DDL/DELETE/TRUNCATE и получает только необходимые
  `SELECT`, `INSERT` и column-level `UPDATE` права.

Provisioning и grants воспроизводятся командами `scripts/postgres_roles.py provision` и
`scripts/postgres_roles.py grant-runtime`. Пароли передаются только через
`ELOWYN_OWNER_PASSWORD`/`ELOWYN_RUNTIME_PASSWORD`; скрипт их не печатает. После каждой новой
миграции runtime grants нужно применять повторно.

## DB safety verification

- Полный invariant audit: `docs/DATA_INVARIANT_AUDIT_V0_1.md`.
- Read-only verifier: `elowyn.support.consistency.ConsistencyVerifier`.
- Git history/worktree audit: `python scripts/secret_audit.py`.
- Dump/restore drill: `scripts/recovery_drill.py`.

Recovery drill отказывается работать с обычным `DATABASE_URL`: имя целевой БД должно начинаться с
`elowyn_recovery_`, содержать test marker, а процесс обязан явно задать
`ELOWYN_ALLOW_DESTRUCTIVE_TEST_DB=YES`.

Acceptance contract находится в `ACCEPTANCE_V0_1.md`.
