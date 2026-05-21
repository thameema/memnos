"""
engram_gateway.whatsapp.webhook — FastAPI webhook receiver for Evolution API.

Evolution API delivers incoming WhatsApp messages as POST requests to the
configured webhook URL.  This module provides a FastAPI router that:

1. Accepts the webhook POST immediately (Evolution API requires a fast response).
2. Queues the message processing as a background task.
3. Sends the result back via the EvolutionClient.

Expected Evolution API payload format (relevant fields):
    {
      "event": "messages.upsert",
      "instance": "default",
      "data": {
        "key": {"remoteJid": "15551234567@s.whatsapp.net", "fromMe": false},
        "message": {"conversation": "Hello!"}
      }
    }
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from fastapi import APIRouter, BackgroundTasks, Request, Response

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/webhook", tags=["whatsapp-webhook"])

# WhatsApp message length that triggers file delivery
_WA_MAX_LEN = 3500


def _extract_message(payload: dict) -> tuple[str | None, str | None]:
    """
    Extract phone number and text from an Evolution API webhook payload.

    Returns
    -------
    tuple[phone, text]
        Both values are ``None`` if the payload doesn't carry a user text message.
    """
    event = payload.get("event", "")
    if event not in ("messages.upsert", "MESSAGES_UPSERT", "messages.update"):
        return None, None

    data = payload.get("data", {})
    if not isinstance(data, dict):
        return None, None

    key = data.get("key", {})
    # Ignore messages sent by us (fromMe=True)
    if key.get("fromMe", False):
        return None, None

    remote_jid: str = key.get("remoteJid", "") or ""
    if not remote_jid:
        return None, None

    # Normalise: strip @s.whatsapp.net / @g.us suffixes for phone
    phone = remote_jid.split("@")[0]
    if "-" in phone:
        # Group JIDs look like "<timestamp>-<phone>@g.us" — skip group messages
        return None, None

    # Extract text from various message types
    message = data.get("message", {}) or {}
    text = (
        message.get("conversation")
        or message.get("extendedTextMessage", {}).get("text")
        or message.get("imageMessage", {}).get("caption")
        or message.get("documentMessage", {}).get("caption")
    )
    if not text or not isinstance(text, str):
        return None, None

    return phone, text.strip()


async def process_whatsapp_message(
    phone: str,
    text: str,
    app_state,
) -> None:
    """
    Background task: run the orchestrator and reply via Evolution API.

    Parameters
    ----------
    phone:
        Sender's phone number (used as identifier and reply address).
    text:
        Message text from the sender.
    app_state:
        ``request.app.state`` — carries ``config``, ``orchestrator``, etc.
    """
    config = getattr(app_state, "config", None)
    orchestrator = getattr(app_state, "orchestrator", None)

    if orchestrator is None:
        logger.error("process_whatsapp_message: orchestrator not in app.state")
        return

    # Resolve gateway config
    gateway_cfg = getattr(config, "gateway", None)
    wa_cfg = getattr(gateway_cfg, "whatsapp", None)

    base_url = getattr(wa_cfg, "evolution_api_url", "http://localhost:8080") if wa_cfg else "http://localhost:8080"
    api_key = getattr(wa_cfg, "evolution_api_key", "") if wa_cfg else ""
    default_namespace = getattr(wa_cfg, "default_namespace", "personal:default") if wa_cfg else "personal:default"

    # Use phone as user-scoped namespace identifier
    namespace = f"personal:{phone}"

    # Check allowed phones if configured
    allowed_phones = getattr(wa_cfg, "allowed_phones", []) if wa_cfg else []
    if allowed_phones and phone not in [str(p) for p in allowed_phones]:
        logger.warning("WhatsApp message from non-allowed phone: %s", phone)
        return

    from engram_gateway.whatsapp.evolution_client import EvolutionClient  # noqa: PLC0415

    evo_client = EvolutionClient(base_url=base_url, api_key=api_key)

    try:
        logger.debug(
            "WhatsApp message from phone=%s ns=%s text=%r", phone, namespace, text[:120]
        )

        task = await orchestrator.run(text, namespace)

        result_text = ""
        if task is not None:
            result_text = str(getattr(task, "result", "") or "")
            if not result_text:
                error = getattr(task, "error", None)
                result_text = f"Error: {error}" if error else "Task completed with no output."
        else:
            result_text = "No result returned."

        if len(result_text) <= _WA_MAX_LEN:
            await evo_client.send_text(phone, result_text)
        else:
            # Send a short preview then the full result as a document
            preview = result_text[:_WA_MAX_LEN] + "\n\n[Full result sent as attachment]"
            await evo_client.send_text(phone, preview)

            timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
            filename = f"engram_result_{timestamp}.txt"
            await evo_client.send_file(phone, filename, result_text.encode("utf-8"))

    except Exception as exc:
        logger.exception("process_whatsapp_message failed for phone=%s: %s", phone, exc)
        try:
            await evo_client.send_text(phone, f"Sorry, an error occurred: {exc}")
        except Exception:
            pass
    finally:
        await evo_client.close()


@router.post("/whatsapp")
async def whatsapp_webhook(
    payload: dict,
    background_tasks: BackgroundTasks,
    request: Request,
) -> Response:
    """
    Receive an incoming webhook POST from Evolution API.

    Returns HTTP 200 immediately so Evolution API doesn't time out.  The
    actual message processing happens asynchronously in a background task.
    """
    logger.debug("WhatsApp webhook received event=%s", payload.get("event"))

    phone, text = _extract_message(payload)
    if phone is None or text is None:
        # Not a text message we handle — acknowledge and skip
        return Response(status_code=200)

    background_tasks.add_task(
        process_whatsapp_message,
        phone,
        text,
        request.app.state,
    )

    return Response(status_code=200)
