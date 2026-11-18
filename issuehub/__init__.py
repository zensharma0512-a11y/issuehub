"""IssueHub: a concurrency-safe, multi-project issue tracker service."""

from .errors import (
    AlreadyExists,
    AuthenticationError,
    IdempotencyConflict,
    InvalidTransition,
    IssueHubError,
    NotFound,
    PermissionDenied,
    ValidationError,
    VersionConflict,
)
from .service import IssueService, Role, Status

__all__ = [
    "AlreadyExists",
    "AuthenticationError",
    "IdempotencyConflict",
    "InvalidTransition",
    "IssueHubError",
    "IssueService",
    "NotFound",
    "PermissionDenied",
    "Role",
    "Status",
    "ValidationError",
    "VersionConflict",
]
