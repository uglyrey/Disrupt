"""Advertising starts must never be mistaken for account-binding or sign-in tokens."""

import asyncio

import pytest

from tests.telegram_adapter.test_adapter import message, session
from tests.telegram_adapter.test_login import LOGIN_TOKEN
from tests.telegram_adapter.test_login import settings as settings


def test_campaign_start_preserves_source_without_linking_or_saving(settings):
    async def run():
        bot, core, telegram, calls, replies, _ = await session(settings, [message(10, "/start src_pilot")])
        async with core, telegram:
            await bot.poll_once()
            assert calls == []
            button = replies[0]["reply_markup"]["inline_keyboard"][0][0]
            assert button["url"] == "https://example.test?utm_source=pilot"
            assert "callback_data" not in button

    asyncio.run(run())


@pytest.mark.parametrize("payload", ["src_", "src_../x", "src_" + "a" * 61, "src_тест"])
def test_invalid_campaign_is_never_processed_as_a_binding_code(settings, payload):
    async def run():
        bot, core, telegram, calls, replies, _ = await session(settings, [message(10, "/start " + payload)])
        async with core, telegram:
            await bot.poll_once()
            assert calls == []
            assert "некорректна" in replies[0]["text"]

    asyncio.run(run())


def test_login_start_still_requires_its_own_confirmation(settings):
    async def run():
        bot, core, telegram, calls, replies, _ = await session(
            settings, [message(10, "/start login_" + LOGIN_TOKEN)]
        )
        async with core, telegram:
            await bot.poll_once()
            assert calls == []
            yes = replies[0]["reply_markup"]["inline_keyboard"][0][0]
            assert yes["callback_data"] == "login:" + LOGIN_TOKEN

    asyncio.run(run())


def test_legacy_binding_nonce_that_begins_with_src_is_still_a_binding(settings):
    async def run():
        code = "src_" + "a" * 39  # A valid 43-character legacy nonce, never a campaign.
        bot, core, telegram, calls, replies, _ = await session(settings, [message(10, "/start " + code)])
        async with core, telegram:
            await bot.poll_once()
            assert calls == []
            yes = replies[0]["reply_markup"]["inline_keyboard"][0][0]
            assert yes["callback_data"] == "link:" + code

    asyncio.run(run())


def test_longest_campaign_does_not_enter_the_legacy_binding_namespace(settings):
    async def run():
        source = "a" * 17
        bot, core, telegram, calls, replies, _ = await session(
            settings, [message(10, "/start src_" + source)]
        )
        async with core, telegram:
            await bot.poll_once()
            assert calls == []
            yes = replies[0]["reply_markup"]["inline_keyboard"][0][0]
            assert yes["url"] == "https://example.test?utm_source=" + source

    asyncio.run(run())
