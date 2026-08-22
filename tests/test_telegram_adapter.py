from elowyn.transport.telegram import TelegramAdapter


def test_telegram_adapter_is_deny_by_default() -> None:
    assert TelegramAdapter().check_user(1001) is False


def test_telegram_adapter_allows_only_configured_user() -> None:
    adapter = TelegramAdapter(allowed_user_id=1001)
    assert adapter.check_user(1001) is True
    assert adapter.check_user(1002) is False
