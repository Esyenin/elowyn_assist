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
- `ELOWYN_MODEL` — model string, понятный Pydantic AI; provider credentials задаются стандартными
  переменными выбранного provider.

`TELEGRAM_ALLOWED_USER_ID` обязателен: production entrypoint работает deny-by-default, а не открывает
персонального ассистента всем пользователям бота.

## Тесты

Быстрый contract/domain набор не требует PostgreSQL:

```bash
PYTHONPATH=src pytest -q tests/test_schema_contract.py tests/test_world_state_service.py
```

Полный suite:

```bash
PYTHONPATH=src pytest -q
```

Conversation evals требуют `pydantic-ai`. PostgreSQL acceptance 1–9 требуют отдельную test DB:

```bash
TEST_DATABASE_URL=postgresql+asyncpg://... PYTHONPATH=src pytest -q -m postgres
```

Acceptance contract находится в `ACCEPTANCE_V0_1.md`.
