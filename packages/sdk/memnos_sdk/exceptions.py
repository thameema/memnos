from __future__ import annotations


class MemnosError(Exception):
    """Base exception for all memnos SDK errors."""


class AuthenticationError(MemnosError):
    """Raised on HTTP 401 or 403 responses."""


class NotFoundError(MemnosError):
    """Raised on HTTP 404 responses."""


class ValidationError(MemnosError):
    """Raised on HTTP 422 responses."""


class ServerError(MemnosError):
    """Raised on HTTP 5xx responses."""


class ConnectionError(MemnosError):
    """Raised when a network-level failure prevents the request from completing."""
