# Acceptance contract — Elowyn v0.1

Версия 0.1 считается законченной только после прохождения вертикальных сценариев ниже.

1. **Persistence.** Через Telegram создаётся Project/Task/Goal; после полного restart backend Elowyn
   корректно восстанавливает текущее состояние из PostgreSQL.
2. **Natural-language update.** Однозначная фраза пользователя меняет существующую сущность без
   отдельной CRUD-команды; изменение проходит через domain tool/Core.
3. **History + provenance.** Изменение дедлайна сохраняет новое текущее значение, Event с old/new и
   Source, указывающий на исходный Message.
4. **Correction.** «Нет, я имел в виду 28-е» исправляет состояние и создаёт новый Event; прошлый Event
   остаётся в истории.
5. **Undo.** «Верни как было до прошлого сообщения» создаёт обратное изменение; историческое событие
   не удаляется.
6. **Decision lifecycle.** Значимый выбор записывается как Decision с alternative/reasoning summary;
   пересмотр создаёт новое Decision и переводит старое в SUPERSEDED.
7. **Relations.** Task может иметь parent Task, optional primary Project, несколько Goal и dependency;
   semantic relation принимает только фиксированный RelationType.
8. **Assistant inference.** Elowyn может записать importance/estimate со Source типа
   ASSISTANT_INFERENCE и confidence; прямое исправление пользователя заменяет текущее значение через
   новый Event.
9. **Validation boundary.** Невалидный domain command не меняет World State и не создаёт Domain Event.
10. **Conversation UX.** Пользователь не обязан видеть entity_id/SQL/CRUD; Elowyn отвечает смыслом.

Пункты 1–9 должны иметь автоматизированные integration/acceptance tests. Пункт 10 проверяется набором
conversation eval cases после подключения Pydantic AI TestModel/Model provider.
