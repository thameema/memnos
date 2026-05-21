"""
engram_gateway.telegram.bot — Telegram bot gateway using python-telegram-bot v21.

The bot accepts natural-language messages and routes them through the engram
orchestrator.  It also exposes convenience commands for memory search and task
status lookup.

Supported commands
------------------
/start        — welcome message
/help         — list available commands
/memory       — memory operations: /memory search <query> | /memory list
/task         — task status: /task status <id>
/ns           — switch active namespace: /ns personal:default

Message flow
------------
1.  User sends a message.
2.  Bot replies "Working…" immediately.
3.  Message is passed to orchestrator.run(text, namespace).
4.  "Working…" reply is edited with the result.
5.  If result > 4000 chars, full result is sent as a .txt attachment.
"""

from __future__ import annotations

import asyncio
import io
import logging
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

try:
    from telegram import Update
    from telegram.constants import ParseMode
    from telegram.ext import (
        Application,
        CommandHandler,
        ContextTypes,
        MessageHandler,
        filters,
    )
    _TELEGRAM_AVAILABLE = True
except ImportError:
    _TELEGRAM_AVAILABLE = False
    logger.warning("python-telegram-bot not installed; Telegram gateway disabled")

from engram_gateway.telegram.formatter import (
    format_result,
    format_search_results,
    format_task_status,
)

# Maximum message length before we switch to file attachment
_MAX_MSG_LEN = 4000
# How often (seconds) to send "still thinking…" updates for long tasks
_THINKING_INTERVAL = 45


class TelegramGateway:
    """
    Telegram bot gateway that forwards user messages to the engram orchestrator.

    Parameters
    ----------
    token:
        Telegram bot token from @BotFather.
    allowed_users:
        List of Telegram user IDs permitted to use the bot.
        If empty, *all* users are allowed (unsafe for public deployments).
    orchestrator:
        engram Orchestrator instance.
    client:
        EngramClient instance (used for direct memory commands).
    default_namespace:
        Namespace to use for messages that don't specify one explicitly.
    """

    def __init__(
        self,
        token: str,
        allowed_users: list[int],
        orchestrator,
        client,
        default_namespace: str = "personal:default",
    ) -> None:
        if not _TELEGRAM_AVAILABLE:
            raise RuntimeError(
                "python-telegram-bot is not installed. "
                "Install it with: pip install python-telegram-bot>=21.0"
            )
        self.token = token
        self.allowed_users = set(int(u) for u in allowed_users) if allowed_users else set()
        self.orchestrator = orchestrator
        self.client = client
        self.default_namespace = default_namespace
        self.app: Application | None = None
        # Per-user active namespace (overrideable with /ns)
        self._user_namespace: dict[int, str] = {}

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Initialise and start polling for updates."""
        self.app = (
            Application.builder()
            .token(self.token)
            .build()
        )

        self.app.add_handler(CommandHandler("start", self.cmd_start))
        self.app.add_handler(CommandHandler("help", self.cmd_help))
        self.app.add_handler(CommandHandler("memory", self.cmd_memory))
        self.app.add_handler(CommandHandler("task", self.cmd_task))
        self.app.add_handler(CommandHandler("ns", self.cmd_ns))
        self.app.add_handler(
            MessageHandler(filters.TEXT & ~filters.COMMAND, self.handle_message)
        )

        await self.app.initialize()
        await self.app.start()
        await self.app.updater.start_polling(drop_pending_updates=True)
        logger.info("Telegram bot started (polling)")

    async def stop(self) -> None:
        """Gracefully stop the bot."""
        if self.app is None:
            return
        try:
            await self.app.updater.stop()
            await self.app.stop()
            await self.app.shutdown()
            logger.info("Telegram bot stopped")
        except Exception as exc:
            logger.warning("Error stopping Telegram bot: %s", exc)

    # ------------------------------------------------------------------
    # Access control
    # ------------------------------------------------------------------

    def _is_allowed(self, user_id: int) -> bool:
        """Return True if the user is permitted to interact with the bot."""
        if not self.allowed_users:
            return True  # no allowlist → open access
        return user_id in self.allowed_users

    def _get_namespace(self, user_id: int) -> str:
        """Return the active namespace for a user."""
        return self._user_namespace.get(user_id, self.default_namespace)

    # ------------------------------------------------------------------
    # Command handlers
    # ------------------------------------------------------------------

    async def cmd_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle /start."""
        if update.effective_user is None or update.message is None:
            return
        user_id = update.effective_user.id
        if not self._is_allowed(user_id):
            await update.message.reply_text("Access denied.")
            return
        await update.message.reply_text(
            "👋 *engram* is ready.\n\n"
            "Send me any message and I'll think it through using your persistent memory.\n\n"
            "Type /help for available commands.",
            parse_mode=ParseMode.MARKDOWN,
        )

    async def cmd_help(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle /help."""
        if update.effective_user is None or update.message is None:
            return
        if not self._is_allowed(update.effective_user.id):
            await update.message.reply_text("Access denied.")
            return
        help_text = (
            "*engram commands*\n\n"
            "/memory search <query> — search your memory\n"
            "/memory list — recent memories\n"
            "/task status <id> — check a task\n"
            "/ns <namespace> — switch namespace\n"
            "/help — this message\n\n"
            "Or just send any text to start a task."
        )
        await update.message.reply_text(help_text, parse_mode=ParseMode.MARKDOWN)

    async def cmd_memory(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle /memory search <query> and /memory list."""
        if update.effective_user is None or update.message is None:
            return
        user_id = update.effective_user.id
        if not self._is_allowed(user_id):
            await update.message.reply_text("Access denied.")
            return

        args = context.args or []
        namespace = self._get_namespace(user_id)

        if not args:
            await update.message.reply_text(
                "Usage: /memory search <query>  or  /memory list",
                parse_mode=ParseMode.MARKDOWN,
            )
            return

        sub_command = args[0].lower()

        if sub_command == "search":
            if len(args) < 2:
                await update.message.reply_text("Usage: /memory search <query>")
                return
            query = " ".join(args[1:])
            thinking = await update.message.reply_text("_Searching…_", parse_mode=ParseMode.MARKDOWN)
            try:
                results = await self.client.search(query, namespace, top_k=10, mode="hybrid")
                serialised = []
                if results:
                    for r in results:
                        memory = r.memory if hasattr(r, "memory") else r
                        serialised.append({
                            "content": memory.content,
                            "score": float(getattr(r, "score", 0.0)),
                            "created_at": memory.created_at.isoformat()
                            if hasattr(memory.created_at, "isoformat") else str(memory.created_at),
                            "id": str(memory.id),
                        })
                text = format_search_results(serialised)
            except Exception as exc:
                logger.exception("Memory search failed: %s", exc)
                text = f"Search failed: {exc}"
            await thinking.edit_text(text, parse_mode=ParseMode.MARKDOWN)

        elif sub_command == "list":
            thinking = await update.message.reply_text("_Loading recent memories…_", parse_mode=ParseMode.MARKDOWN)
            try:
                results = await self.client.search("", namespace, top_k=10, mode="vector")
                serialised = []
                if results:
                    for r in results:
                        memory = r.memory if hasattr(r, "memory") else r
                        serialised.append({
                            "content": memory.content,
                            "score": float(getattr(r, "score", 0.0)),
                            "created_at": memory.created_at.isoformat()
                            if hasattr(memory.created_at, "isoformat") else str(memory.created_at),
                            "id": str(memory.id),
                        })
                text = format_search_results(serialised)
            except Exception as exc:
                logger.exception("Memory list failed: %s", exc)
                text = f"Failed to list memories: {exc}"
            await thinking.edit_text(text, parse_mode=ParseMode.MARKDOWN)

        else:
            await update.message.reply_text(
                f"Unknown memory sub-command: {sub_command!r}\n"
                "Use: /memory search <query>  or  /memory list"
            )

    async def cmd_task(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle /task status <id>."""
        if update.effective_user is None or update.message is None:
            return
        user_id = update.effective_user.id
        if not self._is_allowed(user_id):
            await update.message.reply_text("Access denied.")
            return

        args = context.args or []
        if len(args) < 2 or args[0].lower() != "status":
            await update.message.reply_text("Usage: /task status <task_id>")
            return

        task_id = args[1]
        try:
            task = await self.orchestrator.get_result(task_id, wait=False)
        except Exception as exc:
            await update.message.reply_text(f"Failed to get task: {exc}")
            return

        if task is None:
            await update.message.reply_text(f"Task `{task_id}` not found.", parse_mode=ParseMode.MARKDOWN)
            return

        status = str(getattr(task, "status", "UNKNOWN"))
        if hasattr(task.status, "value"):
            status = task.status.value
        result = getattr(task, "result", None)
        error = getattr(task, "error", None)
        text = format_task_status(task_id, status, result, error)
        await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)

    async def cmd_ns(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle /ns <namespace> — switch active namespace."""
        if update.effective_user is None or update.message is None:
            return
        user_id = update.effective_user.id
        if not self._is_allowed(user_id):
            await update.message.reply_text("Access denied.")
            return

        args = context.args or []
        if not args:
            current = self._get_namespace(user_id)
            await update.message.reply_text(
                f"Current namespace: `{current}`\nUsage: /ns <namespace>",
                parse_mode=ParseMode.MARKDOWN,
            )
            return

        new_ns = args[0]
        self._user_namespace[user_id] = new_ns
        await update.message.reply_text(
            f"Namespace switched to `{new_ns}`", parse_mode=ParseMode.MARKDOWN
        )

    # ------------------------------------------------------------------
    # Message handler (main orchestration path)
    # ------------------------------------------------------------------

    async def handle_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Process an incoming text message through the engram orchestrator."""
        if update.effective_user is None or update.message is None or update.message.text is None:
            return

        user_id = update.effective_user.id
        if not self._is_allowed(user_id):
            await update.message.reply_text("Access denied.")
            return

        text = update.message.text.strip()
        namespace = self._get_namespace(user_id)

        logger.debug(
            "Telegram message from user_id=%d ns=%s text=%r", user_id, namespace, text[:120]
        )

        # Detect corrections and log them (learning hook)
        from engram_gateway.telegram.formatter import format_result as _fmt  # already imported
        try:
            from engram_learning.feedback import detect_correction  # type: ignore
            is_correction = detect_correction(text)
        except ImportError:
            is_correction = False

        if is_correction:
            logger.info(
                "Correction detected from user_id=%d in ns=%s: %r", user_id, namespace, text[:120]
            )

        # Send "Working…" message so user gets immediate feedback
        working_msg = await update.message.reply_text(
            "_Working…_", parse_mode=ParseMode.MARKDOWN
        )

        # Start a background "still thinking" heartbeat
        thinking_task = asyncio.create_task(
            self._send_thinking_updates(working_msg),
            name=f"thinking-{user_id}",
        )

        try:
            task = await self.orchestrator.run(text, namespace)
            result_text: str = ""
            if task is not None:
                result_text = str(getattr(task, "result", "") or "")
                if not result_text:
                    error = getattr(task, "error", None)
                    if error:
                        result_text = f"Task failed: {error}"
                    else:
                        result_text = "Task completed with no output."
            else:
                result_text = "No result returned."
        except Exception as exc:
            logger.exception("Orchestrator run failed: %s", exc)
            result_text = f"Error: {exc}"
        finally:
            thinking_task.cancel()
            try:
                await thinking_task
            except asyncio.CancelledError:
                pass

        # Edit the "Working…" message or send as attachment
        if len(result_text) <= _MAX_MSG_LEN:
            try:
                await working_msg.edit_text(
                    format_result(result_text), parse_mode=ParseMode.MARKDOWN
                )
            except Exception:
                # Fall back to plain text if Markdown parse fails
                await working_msg.edit_text(result_text)
        else:
            # Send truncated preview, then full result as file
            preview = format_result(result_text[:_MAX_MSG_LEN])
            try:
                await working_msg.edit_text(preview, parse_mode=ParseMode.MARKDOWN)
            except Exception:
                await working_msg.edit_text(result_text[:_MAX_MSG_LEN])

            timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
            filename = f"engram_result_{timestamp}.txt"
            file_bytes = result_text.encode("utf-8")
            await update.message.reply_document(
                document=io.BytesIO(file_bytes),
                filename=filename,
                caption="Full result (too long for a single message).",
            )

    async def _send_thinking_updates(self, message) -> None:
        """Periodically edit the working message to reassure the user."""
        dots = 1
        while True:
            await asyncio.sleep(_THINKING_INTERVAL)
            dot_str = "." * dots
            try:
                await message.edit_text(
                    f"_Still thinking{dot_str}_", parse_mode=ParseMode.MARKDOWN
                )
            except Exception:
                pass  # message may have already been edited
            dots = (dots % 3) + 1
