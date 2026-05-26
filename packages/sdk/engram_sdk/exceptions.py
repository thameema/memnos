from __future__ import annotations


class EngramError(Exception):
    """Base exception for all engram SDK errors."""


class AuthenticationError(EngramError):
    """Raised on HTTP 401 or 403 responses."""


class NotFoundError(EngramError):
    """Raised on HTTP 404 responses."""


class ValidationError(EngramError):
    """Raised on HTTP 422 responses."""


class ServerError(EngramError):
    """Raised on HTTP 5xx responses."""


class ConnectionError(EngramError):
    """Raised when a network-level failure prevents the request from completing."""
