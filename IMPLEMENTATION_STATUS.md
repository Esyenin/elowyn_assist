# Implementation and DB safety status — 2026-08-22

## Текущий статус

Функциональный contract v0.1 и DB safety phases 1–6 подтверждены локально на реальном PostgreSQL.
Deterministic E2E и реальный hosted-provider vertical подтверждены: `Telegram Bot API/polling →
TelegramAdapter → Pydantic AI → NVIDIA Nemotron → domain tools → least-privilege runtime role →
PostgreSQL → Telegram response`. Функциональных runtime gates до release v0.1.0 не осталось;
merge feature-ветки и release tag выполняются только отдельными командами.

Memory v0.2 Slice 1–10 merge-нуты в `main` explicit non-fast-forward merge; behavioral
acceptance прошёл 13/13 на real Hindsight 0.9.1, а main release gate — 130/130 без skip.
Детали, rebuild/recovery result и честные ограничения зафиксированы в
`docs/specs/V0_2_ACCEPTANCE.md`.

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
- GitHub Actions PostgreSQL service, migration round-trip, Ruff, mypy, compile и полный pytest suite.
- GitHub Actions `checkout/setup-python` обновлены до v7 (Node 24); у workflow минимальные
  `contents: read` permissions.
- Alembic `0002_structural_invariants`: row-local DB constraints для непустых обязательных полей,
  date/type и lifecycle timestamp pairs, self-links, единственного Decision successor, единственного
  Source на user Message и единственного undo на Event.
- Direct-SQL negative tests доказывают, что PostgreSQL самостоятельно отвергает structural
  corruption.
- PostgreSQL `SELECT FOR UPDATE` для конфликтующих state mutations; стабильный UUID lock ordering,
  shared/exclusive advisory lock для точного global undo и сериализация typed graph edits без
  глобальной смены isolation level.
- Раздельные migration owner/runtime роли; runtime не имеет DDL, TRUNCATE, DELETE и mutation rights
  на `events`, `operations`, `sources`.
- Guarded custom-format `pg_dump → DROP только recovery test DB → CREATE → pg_restore` drill с
  полным deterministic state/history digest и post-restore update/correction/undo.
- Read-only consistency verifier для entity/type, relations, Event/Operation, Message/Source,
  supersede и inference provenance; verifier чист после 1000 domain mutations.
- Git history/worktree secret audit и synthetic-canary tests для prompt/log/exception leakage;
  SQL bind parameters скрыты.
- Recovery database защищена test-name/env/server checks и постоянным sentinel comment; runtime и
  owner роли очищаются от memberships, имеют `NOINHERIT`/`NOBYPASSRLS`, runtime лишён `TEMP`.
- CI получает полную Git history и принудительно падает при любом pytest skip/xfail/xpass.
- Runtime-only NVIDIA provider использует OpenAI-compatible hosted endpoint; model/key остаются
  конфигурацией окружения и не протекают в Core.
- Локальная обычная `elowyn_dev` DB имеет отдельные migration owner/application runtime роли;
  Telegram entrypoint загружает `.env` до создания DB engine/provider resources.

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
11. Два concurrent Task update создавали lost update и ложный `Event.old`.
12. Concurrent update + undo мог отменить не последнее фактическое состояние и записать ложную
    историю.
13. Два Decision supersede и duplicate relation creation завершались constraint error вместо
    корректной сериализации/idempotency.
14. Одновременные parent/dependency изменения могли создать цикл, хотя оба Core-check проходили.
15. Первичная передача БД migration owner-роли не передавала владение оставшейся
    `alembic_version`.
16. Locking `events` для undo требовал UPDATE privilege и конфликтовал с append-only runtime model.
17. `TelegramAdapter` отдельно от production entrypoint оставался allow-all при пустой настройке.
18. Safety fixture оставлял setup-строки direct-SQL negative tests между модулями.
19. Global undo имел TOCTOU между выбором последнего Event и блокировкой его Entity при update
    другой Entity.
20. Recovery drill мог удалить существующую test-named БД без постоянного sentinel и не сверял
    server admin/target URL.
21. Runtime роль могла сохранить inherited memberships и получать `TEMP` через `PUBLIC`.
22. Consistency verifier мог autoflush чужие pending ORM changes вызывающей сессии.
23. Shallow checkout и обычный pytest exit code позволяли CI не доказать full-history scan и
    скрыть skips.
24. Production entrypoint передавал model string без NVIDIA custom endpoint и не поддерживал
    локальный `.env` до импорта process-level DB resources.
25. Relative dates передавались LLM без явного текущего времени.
26. Штатный backend restart через `Ctrl+C` печатал полный Python traceback.

## Проверено в текущем окружении

- PostgreSQL: **18.3**, отдельный acceptance cluster на `127.0.0.1:55433`.
- `alembic downgrade base && alembic upgrade head`: **OK** на PostgreSQL.
- Acceptance 1–9: **9 passed**.
- Pydantic AI conversation eval: **3 passed**.
- Полный `pytest -ra` с owner/runtime URLs и `ELOWYN_FAIL_ON_SKIP=1`: **60 passed**, без skip.
- Concurrency suite: два update, update+undo, два supersede, duplicate relation, concurrent
  parent/dependency cycles и deadlock-sensitive opposite lock order — **OK**.
- Runtime permission suite и deterministic adapter/Pydantic/Core/runtime PostgreSQL chain — **OK**.
- Consistency verifier после **1000 mutations** — **OK**.
- Direct-SQL corruption suite — **OK**.
- Custom-format dump/restore digest и post-restore mutations — **OK**.
- Git history/worktree secret audit — **OK**, неплейсхолдерных секретов не найдено.
- `ruff check .`: **OK**.
- `mypy src`: **OK**.
- `python -m compileall -q src tests scripts`: **OK**.
- Установленные ключевые зависимости: aiogram 3.30.0, asyncpg 0.31.0, Pydantic AI 2.33.0,
  SQLAlchemy 2.0.52, Alembic 1.19.1.
- NVIDIA text smoke и реальный Pydantic `TaskCreate` tool/JSON call — **OK**.
- Real NVIDIA DB cases create/update/ambiguity/correction/undo на отдельной test DB — **OK**.
- Обычная runtime DB `elowyn_dev`: migrations owner + ограниченная `elowyn_dev_runtime` application
  role — **OK**.
- Telegram network smoke A–F: greeting, create, state query, update, backend restart/query и
  ambiguity clarification without mutation — **OK**.
- Replay реального Telegram update не создаёт duplicate Message/Task/Event; foreign-user update
  отсекается aiogram router до handler/persistence — **OK**.
- Telegram responses не содержат entity UUID, SQL/CRUD/tool names или configured credentials —
  **OK**.

## Что ещё не подтверждено

- Secret Scanning / Push Protection: `gh` отсутствует, unauthenticated GitHub API эту настройку не
  раскрывает.
- GitHub Actions на feature-ветке после runtime gate: Ruff/mypy, полный PostgreSQL suite и recovery
  drill — **OK**; ветка ещё не merge-нута в `main`.

## Полученные safety guarantees

- Runtime credentials не позволяют DDL/TRUNCATE/DELETE и не позволяют UPDATE/DELETE history.
- Конфликтующие изменения одного объекта сериализуются до чтения old state; Event old/new,
  Operation и Source соответствуют реально применённому порядку.
- Structural corruption отклоняется DB constraints, а cross-table/graph corruption обнаруживается
  read-only verifier.
- Dump/restore сохраняет полное состояние и историю побайтно-детерминированно на уровне canonical
  digest; восстановленная БД остаётся записываемой через domain service.
- Recovery command имеет тройной guard: PostgreSQL, test marker и `elowyn_recovery_` prefix плюс
  явное opt-in окружения.

## Чего система всё ещё не гарантирует

- Owner/admin credentials способны менять history; append-only гарантия относится к runtime role.
- Нет RLS и multi-user isolation — v0.1 остаётся single-user.
- Нет глобального causal sequence для Event разных concurrently изменяемых entities.
- Graph edits сериализуются консервативно; это корректно для v0.1, но не является масштабируемой
  worker architecture.
- Локальный recovery drill не заменяет production backup retention, off-site storage, PITR и
  регулярную operator-проверку.
- Длительные реальные разговоры и provider availability/SLA не покрываются коротким v0.1 smoke.

## Неблокирующая заметка для roadmap

Перед переходом к нескольким concurrent workers разумно добавить DB-backed порядковый ключ/sequence
для глобального causal ordering Event. Текущая схема этому не препятствует.
