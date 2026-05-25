"""
tools/test_telegram_feedback.py — Unit tests for Telegram 👍/👎 inline feedback.

These tests mock the Telegram API objects so they run without a real bot token
or network connection.  They verify:
- Callback data encoding/decoding
- Inline keyboard is attached to result messages
- handle_feedback_callback records feedback and removes the keyboard
- Fallback to direct memory write when FeedbackService is unavailable
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
_REPO_ROOT = str(Path(__file__).resolve().parent.parent)
import types
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

# ---------------------------------------------------------------------------
# Build minimal telegram stubs so the module can import without the real pkg
# ---------------------------------------------------------------------------

def _build_telegram_stubs() -> None:
    telegram_mod = types.ModuleType("telegram")
    constants_mod = types.ModuleType("telegram.constants")
    ext_mod = types.ModuleType("telegram.ext")

    class _ParseMode:
        MARKDOWN = "Markdown"
    constants_mod.ParseMode = _ParseMode

    class _InlineKeyboardButton:
        def __init__(self, text: str, callback_data: str = ""):
            self.text = text
            self.callback_data = callback_data

    class _InlineKeyboardMarkup:
        def __init__(self, keyboard):
            self.inline_keyboard = keyboard

    class _Update:
        pass

    class _Application:
        pass

    class _CallbackQueryHandler:
        def __init__(self, callback, pattern=None):
            self.callback = callback
            self.pattern = pattern

    class _CommandHandler:
        def __init__(self, cmd, callback):
            pass

    class _ContextTypes:
        DEFAULT_TYPE = None

    class _MessageHandler:
        def __init__(self, filters, callback):
            pass

    class _Filters:
        TEXT = True
        COMMAND = True

        def __and__(self, other):
            return self

        def __invert__(self):
            return self

    telegram_mod.Update = _Update
    telegram_mod.InlineKeyboardButton = _InlineKeyboardButton
    telegram_mod.InlineKeyboardMarkup = _InlineKeyboardMarkup
    ext_mod.Application = _Application
    ext_mod.CallbackQueryHandler = _CallbackQueryHandler
    ext_mod.CommandHandler = _CommandHandler
    ext_mod.ContextTypes = _ContextTypes
    ext_mod.MessageHandler = _MessageHandler
    ext_mod.filters = _Filters()

    sys.modules.setdefault("telegram", telegram_mod)
    sys.modules.setdefault("telegram.constants", constants_mod)
    sys.modules.setdefault("telegram.ext", ext_mod)


_build_telegram_stubs()

# Stub engram_gateway.telegram.formatter (must be stubbed before the real package loads)
formatter_mod = types.ModuleType("engram_gateway.telegram.formatter")
formatter_mod.format_result = lambda text, max_len=4096: text
formatter_mod.format_search_results = lambda results: "results"
formatter_mod.format_task_status = lambda *a, **kw: "status"
sys.modules["engram_gateway.telegram.formatter"] = formatter_mod

# Add gateway package to path so real bot.py is importable
_GW_PATH = _REPO_ROOT + "/packages/gateway"
if _GW_PATH not in sys.path:
    sys.path.insert(0, _GW_PATH)

from engram_gateway.telegram.bot import TelegramGateway, _FB_UP, _FB_DOWN  # noqa: E402


def _make_gateway() -> TelegramGateway:
    orchestrator = MagicMock()
    orchestrator.run = AsyncMock()
    client = MagicMock()
    client.add = AsyncMock()
    gw = TelegramGateway(
        token="fake-token",
        allowed_users=[42],
        orchestrator=orchestrator,
        client=client,
        default_namespace="test:ns",
    )
    return gw


def _make_callback_update(data: str, user_id: int = 42, msg_text: str = "result") -> MagicMock:
    query = MagicMock()
    query.data = data
    query.answer = AsyncMock()
    query.edit_message_text = AsyncMock()
    query.edit_message_reply_markup = AsyncMock()
    query.from_user = MagicMock()
    query.from_user.id = user_id
    query.message = MagicMock()
    query.message.text = msg_text
    query.message.caption = None

    update = MagicMock()
    update.callback_query = query
    return update


class TestCallbackDataFormat(unittest.TestCase):
    def test_thumbs_up_prefix(self):
        self.assertEqual(_FB_UP, "fb:+")

    def test_thumbs_down_prefix(self):
        self.assertEqual(_FB_DOWN, "fb:-")

    def test_callback_data_round_trip(self):
        task_id = "abc123"
        up_data = f"{_FB_UP}:{task_id}"
        down_data = f"{_FB_DOWN}:{task_id}"
        parts_up = up_data.split(":", 2)
        parts_down = down_data.split(":", 2)
        self.assertEqual(parts_up, ["fb", "+", task_id])
        self.assertEqual(parts_down, ["fb", "-", task_id])


class TestFeedbackCallback(unittest.IsolatedAsyncioTestCase):
    async def test_thumbs_up_removes_keyboard(self):
        gw = _make_gateway()
        update = _make_callback_update(f"{_FB_UP}:task-001", msg_text="some result")
        ctx = MagicMock()

        # Stub out FeedbackService import to unavailable
        with patch.dict("sys.modules", {"engram_learning.feedback": None,
                                         "engram_learning.episode_store": None,
                                         "engram_learning.quality_store": None}):
            await gw.handle_feedback_callback(update, ctx)

        update.callback_query.answer.assert_awaited_once()
        update.callback_query.edit_message_text.assert_awaited_once()
        call_kwargs = update.callback_query.edit_message_text.call_args
        # Keyboard should be cleared (reply_markup=None)
        self.assertIsNone(call_kwargs.kwargs.get("reply_markup"))
        # Text should end with thumbs-up emoji
        sent_text = call_kwargs.args[0] if call_kwargs.args else call_kwargs.kwargs.get("text", "")
        self.assertIn("👍", sent_text)

    async def test_thumbs_down_removes_keyboard(self):
        gw = _make_gateway()
        update = _make_callback_update(f"{_FB_DOWN}:task-002", msg_text="another result")
        ctx = MagicMock()

        with patch.dict("sys.modules", {"engram_learning.feedback": None,
                                         "engram_learning.episode_store": None,
                                         "engram_learning.quality_store": None}):
            await gw.handle_feedback_callback(update, ctx)

        update.callback_query.answer.assert_awaited_once()
        update.callback_query.edit_message_text.assert_awaited_once()
        sent_text = update.callback_query.edit_message_text.call_args.args[0]
        self.assertIn("👎", sent_text)

    async def test_fallback_memory_write_on_thumbs_up(self):
        gw = _make_gateway()
        update = _make_callback_update(f"{_FB_UP}:task-999")
        ctx = MagicMock()

        with patch.dict("sys.modules", {"engram_learning.feedback": None,
                                         "engram_learning.episode_store": None,
                                         "engram_learning.quality_store": None}):
            await gw.handle_feedback_callback(update, ctx)

        # client.add should have been called as the fallback
        gw.client.add.assert_awaited_once()
        call_kwargs = gw.client.add.call_args.kwargs
        self.assertIn("task-999", call_kwargs.get("content", ""))
        self.assertIn("thumbs_up", call_kwargs.get("tags", []))

    async def test_fallback_memory_write_on_thumbs_down(self):
        gw = _make_gateway()
        update = _make_callback_update(f"{_FB_DOWN}:task-888")
        ctx = MagicMock()

        with patch.dict("sys.modules", {"engram_learning.feedback": None,
                                         "engram_learning.episode_store": None,
                                         "engram_learning.quality_store": None}):
            await gw.handle_feedback_callback(update, ctx)

        gw.client.add.assert_awaited_once()
        call_kwargs = gw.client.add.call_args.kwargs
        self.assertIn("thumbs_down", call_kwargs.get("tags", []))

    async def test_malformed_callback_data_is_ignored(self):
        gw = _make_gateway()
        update = _make_callback_update("fb:bad")  # only 2 parts
        ctx = MagicMock()

        await gw.handle_feedback_callback(update, ctx)

        # answer() is still called (dismiss spinner) but no edit
        update.callback_query.answer.assert_awaited_once()
        update.callback_query.edit_message_text.assert_not_awaited()

    async def test_null_callback_query_is_ignored(self):
        gw = _make_gateway()
        update = MagicMock()
        update.callback_query = None
        ctx = MagicMock()

        # Should not raise
        await gw.handle_feedback_callback(update, ctx)


class TestInlineKeyboardInHandleMessage(unittest.IsolatedAsyncioTestCase):
    """Verify that handle_message attaches the feedback keyboard to results."""

    async def test_feedback_keyboard_attached_to_short_result(self):
        gw = _make_gateway()

        task = MagicMock()
        task.id = "t-123"
        task.result = "short answer"
        task.error = None
        gw.orchestrator.run = AsyncMock(return_value=task)

        # Build a minimal fake update/message
        msg = MagicMock()
        msg.text = "hello engram"
        working_msg = MagicMock()
        working_msg.edit_text = AsyncMock()
        msg.reply_text = AsyncMock(return_value=working_msg)

        user = MagicMock()
        user.id = 42

        update = MagicMock()
        update.effective_user = user
        update.message = msg

        ctx = MagicMock()

        await gw.handle_message(update, ctx)

        # edit_text should have been called with a reply_markup (the keyboard)
        working_msg.edit_text.assert_awaited()
        call_kwargs = working_msg.edit_text.call_args.kwargs
        reply_markup = call_kwargs.get("reply_markup")
        self.assertIsNotNone(reply_markup, "reply_markup should be set on result message")
        # keyboard has one row with two buttons
        buttons = reply_markup.inline_keyboard[0]
        self.assertEqual(len(buttons), 2)
        self.assertEqual(buttons[0].text, "👍")
        self.assertEqual(buttons[1].text, "👎")


if __name__ == "__main__":
    unittest.main(verbosity=2)
