from __future__ import annotations

from typing import Any

import httpx

from engram_sdk.exceptions import (
    AuthenticationError,
    ConnectionError,
    NotFoundError,
    ServerError,
    ValidationError,
)


def _raise_for_status(response: httpx.Response) -> None:
    code = response.status_code
    if code in (401, 403):
        raise AuthenticationError(f"HTTP {code}: {response.text}")
    if code == 404:
        raise NotFoundError(f"HTTP 404: {response.text}")
    if code == 422:
        raise ValidationError(f"HTTP 422: {response.text}")
    if code >= 500:
        raise ServerError(f"HTTP {code}: {response.text}")
    response.raise_for_status()


class _SyncTransport:
    def __init__(self, base_url: str, api_key: str, timeout: float = 30.0) -> None:
        self._client = httpx.Client(
            base_url=base_url,
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=timeout,
        )

    def get(self, path: str, params: dict[str, Any] | None = None) -> dict:
        try:
            response = self._client.get(path, params=params)
        except httpx.TransportError as exc:
            raise ConnectionError(str(exc)) from exc
        _raise_for_status(response)
        return response.json()

    def post(self, path: str, json: dict | None = None) -> dict:
        try:
            response = self._client.post(path, json=json)
        except httpx.TransportError as exc:
            raise ConnectionError(str(exc)) from exc
        _raise_for_status(response)
        return response.json()

    def delete(self, path: str) -> None:
        try:
            response = self._client.delete(path)
        except httpx.TransportError as exc:
            raise ConnectionError(str(exc)) from exc
        _raise_for_status(response)

    def close(self) -> None:
        self._client.close()


class _AsyncTransport:
    def __init__(self, base_url: str, api_key: str, timeout: float = 30.0) -> None:
        self._client = httpx.AsyncClient(
            base_url=base_url,
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=timeout,
        )

    async def get(self, path: str, params: dict[str, Any] | None = None) -> dict:
        try:
            response = await self._client.get(path, params=params)
        except httpx.TransportError as exc:
            raise ConnectionError(str(exc)) from exc
        _raise_for_status(response)
        return response.json()

    async def post(self, path: str, json: dict | None = None) -> dict:
        try:
            response = await self._client.post(path, json=json)
        except httpx.TransportError as exc:
            raise ConnectionError(str(exc)) from exc
        _raise_for_status(response)
        return response.json()

    async def delete(self, path: str) -> None:
        try:
            response = await self._client.delete(path)
        except httpx.TransportError as exc:
            raise ConnectionError(str(exc)) from exc
        _raise_for_status(response)

    async def aclose(self) -> None:
        await self._client.aclose()
