"""Error types. Each carries a stable machine-readable code and an HTTP status."""


class IssueHubError(Exception):
    code = "internal_error"
    http_status = 500

    def __init__(self, message: str, **details):
        super().__init__(message)
        self.message = message
        self.details = details

    def to_dict(self) -> dict:
        body = {"error": self.code, "message": self.message}
        if self.details:
            body["details"] = dict(self.details)
        return body


class ValidationError(IssueHubError):
    code = "validation_error"
    http_status = 400


class AuthenticationError(IssueHubError):
    code = "unauthenticated"
    http_status = 401


class PermissionDenied(IssueHubError):
    code = "forbidden"
    http_status = 403


class NotFound(IssueHubError):
    code = "not_found"
    http_status = 404


class AlreadyExists(IssueHubError):
    code = "already_exists"
    http_status = 409


class VersionConflict(IssueHubError):
    code = "version_conflict"
    http_status = 409


class InvalidTransition(IssueHubError):
    code = "invalid_transition"
    http_status = 409


class IdempotencyConflict(IssueHubError):
    """The same idempotency key was reused with a different request body."""

    code = "idempotency_key_reused"
    http_status = 422


class StorageUnavailable(IssueHubError):
    """The database can't be reached right now. Safe for the client to retry."""

    code = "storage_unavailable"
    http_status = 503


class StorageError(IssueHubError):
    """Any other database failure. Never exposes driver details to clients."""

    code = "storage_error"
    http_status = 500
