# Implementation status — 2026-08-22

## Текущий статус

Код доведён до **v0.1 acceptance candidate**. Локальные schema/domain contract tests зелёные.
Полная версия ещё не помечается как acceptance-certified только потому, что в текущей среде нет
PostgreSQL runtime/dependencies (`asyncpg`, `pydantic-ai`, `aiogram`) и поэтому end-to-end suite
физически не может быть исполнен здесь.

## Реализовано

- Исполняемая ORM-схема v0.1: 17 таблиц + Alembic `0001_initial`.
- Thin `Entity` identity + typed Task/Project/Goal/Decision tables.
- Strict relations: Task parent, Project parent, Task↔Goal, Project↔Goal, Task dependency,
  Task primary Project; cycle/reference validation находится в Core.
- Controlled semantic `EntityRelation` через фиксированный `RelationType`.
- Transport-independent `Conversation` / `Message` persistence и `Message → Source` provenance.
- Idempotent ingestion Telegram messages; повтор после provider failure переисполняется, а повтор уже
  успешно отвеченного turn игнорируется через internal reply marker.
- `WorldStateService` как единственный write boundary для LLM tools; direct SQL tool отсутствует.
- Create/update/correction для Task/Project/Goal с field-level old/new Event changes.
- `Operation` grouping для нескольких domain actions одного natural-language turn.
- Монотонный порядок Event внутри service turn; undo не зависит от случайного UUID tie-breaker.
- Inverse undo для Task/Project/Goal updates и SuccessCriterion evaluation; исходный Event остаётся.
- Decision lifecycle: alternatives + reasoning summary; пересмотр создаёт новое Decision, старое
  переводится в `SUPERSEDED`, Entity получает `superseded_by_entity_id`.
- Assistant inference provenance для Task importance/estimate, Project/Goal importance,
  SuccessCriterion evaluation и inferred semantic relation: `ASSISTANT_INFERENCE`, confidence,
  reason_summary, `SourceDependency` на evidence.
- User correction заменяет current provenance новым user Source, не переписывая историю.
- SuccessCriterion read/update/evaluation path.
- World State snapshot возвращает criteria, Decision alternatives, relations и остальные поля,
  необходимые Elowyn после restart.
- `Project.current_summary` реализован как derived cache: refresh через Core, любой Domain Event
  консервативно инвалидирует cache; cache не создаёт Domain Event и не является source of truth.
- Versioned Identity prompt `Elowyn` остаётся отдельным от Core/domain logic.
- Pydantic AI tool wiring; все DB tools `sequential=True`, поскольку используют один AsyncSession turn.
- Telegram adapter не протекает в Core; production entrypoint требует `TELEGRAM_ALLOWED_USER_ID`
  (deny-by-default).
- Conversation eval contract для ambiguity/no-CRUD UX.
- PostgreSQL acceptance suite для сценариев 1–9.

## Исправленные проблемы исходного scaffold

1. Decision `supersedes_decision_id` раньше не переводил старое Decision в `SUPERSEDED`.
2. Conversation/Message persistence и `Message → Source` отсутствовали в runtime path.
3. Не было update/correction/undo/query/context vertical slice.
4. AI-derived assessment мог терять отдельный inference provenance.
5. Domain reference/cycle invariants не были достаточной validation boundary.
6. Несколько Pydantic AI tools могли конкурентно использовать один SQLAlchemy AsyncSession.
7. SuccessCriterion и DecisionAlternative сохранялись, но не возвращались в LLM World State context.
8. Несколько Event в одной PostgreSQL transaction могли иметь одинаковый `now()` timestamp, из-за
   чего latest/undo мог зависеть от случайного UUID.
9. Повтор Telegram update после provider failure мог быть ошибочно проигнорирован навсегда.
10. Пустой Telegram allow-list фактически открывал personal bot всем пользователям.

## Проверено в текущем окружении

- `PYTHONPATH=src pytest -q`: **17 passed, 2 skipped**.
- `python -m compileall -q src tests`: **OK**.
- Schema/Alembic contract tests: **OK**.
- 2 skipped suites:
  - PostgreSQL acceptance 1–9: нужен `TEST_DATABASE_URL` + `asyncpg` + `pydantic-ai`;
  - conversation eval: нужен `pydantic-ai`.
- `aiogram`, `pydantic-ai`, `asyncpg`, `ruff`, `mypy` отсутствуют в текущем sandbox; сеть для
  установки зависимостей недоступна.

## Что остаётся до формального «v0.1 done»

1. В окружении с зависимостями поднять PostgreSQL, выполнить `alembic upgrade head`.
2. Запустить `TEST_DATABASE_URL=... PYTHONPATH=src pytest -q -m postgres` и добиться 9/9 green.
3. Запустить conversation eval с Pydantic AI; затем хотя бы smoke-turn с выбранным реальным provider.
4. Запустить `ruff`/`mypy` после установки dev dependencies и исправить только реальные замечания.

До выполнения этих пунктов код является implementation-complete candidate, но не утверждается как
полностью прошедшая acceptance v0.1.

## Неблокирующая заметка для roadmap

v0.1 сериализует Telegram turns и поддерживает монотонный Event timestamp внутри одного
`WorldStateService`. Перед переходом к нескольким concurrent workers разумно добавить DB-backed
порядковый ключ/sequence для глобального causal ordering Event. Текущая схема этому не препятствует.
