"""
engram_gateway.whatsapp.evolution_client — HTTP client for the Evolution API.

Evolution API is an open-source WhatsApp bridge that exposes a REST API for
sending/receiving WhatsApp messages via the unofficial WhatsApp Web protocol.

Reference: https://doc.evolution-api.com/
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

logger = logging.getLogger(__name__)

_DEFAULT_TIMEOUT = 30.0


class EvolutionClient:
    """
    Async HTTP client for the Evolution API.

    Parameters
    ----------
    base_url:
        Base URL of the Evolution API server (e.g. ``http://localhost:8080``).
    api_key:
        Authentication API key for the Evolution API.
    instance_name:
        Name of the WhatsApp instance registered in Evolution API (default
        ``"default"``).
    """

    def __init__(
        self,
        base_url: str,
        api_key: str,
        instance_name: str = "default",
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.instance_name = instance_name
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            headers={
                "apikey": self.api_key,
                "Content-Type": "application/json",
            },
            timeout=_DEFAULT_TIMEOUT,
        )

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def close(self) -> None:
        """Close the underlying HTTP client."""
        await self._client.aclose()

    # ------------------------------------------------------------------
    # Messaging
    # ------------------------------------------------------------------

    async def send_text(self, phone: str, text: str) -> bool:
        """
        Send a plain-text WhatsApp message.

        Parameters
        ----------
        phone:
            Recipient phone number in E.164 format without ``+``
            (e.g. ``"15551234567"``), or with country code and ``@s.whatsapp.net``
            suffix (Evolution API accepts both).
        text:
            Message text to send.

        Returns
        -------
        bool
            ``True`` if the message was accepted by Evolution API, ``False`` otherwise.
        """
        url = f"/message/sendText/{self.instance_name}"
        payload: dict[str, Any] = {
            "number": phone,
            "textMessage": {"text": text},
        }
        try:
            response = await self._client.post(url, json=payload)
            response.raise_for_status()
            logger.debug(
                "send_text | phone=%s status=%d", phone, response.status_code
            )
            return True
        except httpx.HTTPStatusError as exc:
            logger.warning(
                "send_text failed | phone=%s status=%d body=%s",
                phone,
                exc.response.status_code,
                exc.response.text[:200],
            )
            return False
        except Exception as exc:
            logger.exception("send_text unexpected error | phone=%s: %s", phone, exc)
            return False

    async def send_file(self, phone: str, filename: str, content: bytes) -> bool:
        """
        Send a file (document) via WhatsApp.

        The file content is base64-encoded and posted as a media message to
        Evolution API's ``/message/sendMedia`` endpoint.

        Parameters
        ----------
        phone:
            Recipient phone number.
        filename:
            Name of the file as the recipient will see it.
        content:
            Raw file bytes to send.

        Returns
        -------
        bool
            ``True`` if the message was accepted, ``False`` otherwise.
        """
        import base64
        import mimetypes

        mime_type, _ = mimetypes.guess_type(filename)
        if mime_type is None:
            mime_type = "application/octet-stream"

        encoded = base64.b64encode(content).decode("utf-8")
        url = f"/message/sendMedia/{self.instance_name}"
        payload: dict[str, Any] = {
            "number": phone,
            "mediatype": "document",
            "mimetype": mime_type,
            "caption": filename,
            "media": encoded,
            "fileName": filename,
        }
        try:
            response = await self._client.post(url, json=payload)
            response.raise_for_status()
            logger.debug(
                "send_file | phone=%s filename=%s status=%d",
                phone,
                filename,
                response.status_code,
            )
            return True
        except httpx.HTTPStatusError as exc:
            logger.warning(
                "send_file failed | phone=%s filename=%s status=%d body=%s",
                phone,
                filename,
                exc.response.status_code,
                exc.response.text[:200],
            )
            return False
        except Exception as exc:
            logger.exception(
                "send_file unexpected error | phone=%s filename=%s: %s",
                phone,
                filename,
                exc,
            )
            return False

    async def get_instance_status(self) -> dict:
        """
        Check the connection state of the WhatsApp instance.

        Returns
        -------
        dict
            Status payload from Evolution API, or ``{"state": "error"}`` on failure.
        """
        url = f"/instance/connectionState/{self.instance_name}"
        try:
            response = await self._client.get(url)
            response.raise_for_status()
            return response.json()
        except Exception as exc:
            logger.warning("get_instance_status failed: %s", exc)
            return {"state": "error", "error": str(exc)}
