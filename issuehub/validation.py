"""Input validation helpers. All of them reject wrong types instead of coercing."""

import re

from .errors import AuthenticationError, ValidationError

_IDENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
_IDEMPOTENCY_KEY = re.compile(r"^[\x21-\x7e]{1,128}$")  # printable ASCII, no spaces

PRIORITIES = ("low", "medium", "high", "critical")


def require_actor(actor) -> str:
    if not isinstance(actor, str) or not _IDENT.match(actor):
        raise AuthenticationError("a valid authenticated user is required")
    return actor


def identifier(value, field: str) -> str:
    if not isinstance(value, str) or not _IDENT.match(value):
        raise ValidationError(
            f"{field} must be 1-64 chars of letters, digits, '_', '.', '-'", field=field
        )
    return value


def text(value, field: str, *, min_len: int, max_len: int) -> str:
    if not isinstance(value, str):
        raise ValidationError(f"{field} must be a string", field=field)
    cleaned = value.strip()
    if not (min_len <= len(cleaned) <= max_len):
        raise ValidationError(
            f"{field} must be between {min_len} and {max_len} characters", field=field
        )
    return cleaned


def strict_int(value, field: str, *, minimum: int, maximum: int | None = None) -> int:
    # bool is a subclass of int in Python; True must not be accepted as 1.
    if type(value) is not int:
        raise ValidationError(f"{field} must be an integer", field=field)
    if value < minimum or (maximum is not None and value > maximum):
        bound = f">= {minimum}" if maximum is None else f"between {minimum} and {maximum}"
        raise ValidationError(f"{field} must be {bound}", field=field)
    return value


def priority(value) -> str:
    if not isinstance(value, str) or value not in PRIORITIES:
        raise ValidationError(f"priority must be one of {', '.join(PRIORITIES)}", field="priority")
    return value


def idempotency_key(value) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not _IDEMPOTENCY_KEY.match(value):
        raise ValidationError(
            "idempotency key must be 1-128 printable ASCII characters", field="idempotency_key"
        )
    return value
