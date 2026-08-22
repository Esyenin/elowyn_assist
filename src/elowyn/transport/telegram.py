from __future__ import annotations

from elowyn.domain.enums import TransportType
from elowyn.domain.messages import IncomingMessage


class TelegramAdapter:
    """Translate Telegram-specific updates into the transport-independent message model."""

    def __init__(self, allowed_user_id: int | None = None):
        self.allowed_user_id = allowed_user_id

    def check_user(self, telegram_user_id: int) -> bool:
        return self.allowed_user_id is not None and telegram_user_id == self.allowed_user_id

    def to_incoming(self, message) -> IncomingMessage:
        return IncomingMessage(
            transport=TransportType.TELEGRAM,
            external_conversation_id=str(message.chat.id),
            external_message_id=str(message.message_id),
            text=message.text,
            sent_at=message.date,
            raw_payload=message.model_dump(mode="json", exclude_none=True),
        )


def build_router(message_handler, *, adapter: TelegramAdapter | None = None):
    from aiogram import Router
    from aiogram.types import Message as TelegramMessage

    adapter = adapter or TelegramAdapter()
    router = Router(name="elowyn")

    @router.message()
    async def on_message(message: TelegramMessage) -> None:
        if message.from_user is None or not adapter.check_user(message.from_user.id):
            return
        if not message.text:
            await message.answer("В v0.1 я пока принимаю только текстовые сообщения.")
            return
        response = await message_handler(adapter.to_incoming(message))
        if response is not None:
            await message.answer(response)

    return router
