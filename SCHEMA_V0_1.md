# SQL / ORM schema v0.1

## Почему именно так

- `entities` — тонкий supertype identity, а не универсальная JSON-таблица.
- `tasks/projects/goals/decisions` — typed domain tables.
- Строгие связи (`parent`, dependency, Task↔Goal, Project↔Goal) имеют собственные FK/tables.
- `entity_relations` используется только для дополнительной semantic связи из фиксированного каталога.
- `operations/events/sources` отделяют текущую истину от истории и provenance.
- AI-derived importance/estimate ссылаются на `Source`, чтобы current state сохранял происхождение
  рабочих оценок без необходимости каждый раз восстанавливать его из event replay.
- PostgreSQL-native ENUM сознательно не используется: строковые CHECK enums проще эволюционировать
  миграциями. JSONB применяется в PostgreSQL только для `raw_payload` и event `changes`.

## Таблицы

### Core identity
- `entities(id, entity_type, superseded_by_entity_id, removed_at, created_at, updated_at)`

### Domain
- `tasks`
- `projects`
- `goals`
- `success_criteria`
- `decisions`
- `decision_alternatives`

### Strict relations
- `task_goal_links`
- `project_goal_links`
- `task_dependencies`

### Semantic relations
- `entity_relations`

### Conversation/provenance/history
- `conversations`
- `messages`
- `sources`
- `source_dependencies`
- `operations`
- `events`

Полная колонная спецификация является исполняемой: `src/elowyn/db/models.py` — источник истины для
схемы кода, Alembic migration `0001_initial` — первый database revision.
