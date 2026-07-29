from __future__ import annotations

import re
from typing import Any

from fastapi import status

_WIN_PATH_RE = re.compile(r"[A-Za-z]:\\[^\s,;]+")
_MAX_STDERR_PREVIEW_LEN = 500


def sanitize_powershell_text(value: str, *, max_len: int = _MAX_STDERR_PREVIEW_LEN) -> str:
    """Remove high-risk infrastructure details from log/client text."""
    redacted = _WIN_PATH_RE.sub("<path>", value)
    return redacted[:max_len]


class DhcpEnvReason:
    """Machine-readable reason codes returned in API error responses."""
    UNSUPPORTED_OS = "unsupported_os"
    WSL_DETECTED = "wsl_detected"
    POWERSHELL_NOT_FOUND = "powershell_not_found"
    POWERSHELL_EXEC_FAILED = "powershell_exec_failed"
    DHCP_CMDLETS_UNAVAILABLE = "dhcp_cmdlets_unavailable"
    # psrp transport only
    PSRP_DEPENDENCY_MISSING = "psrp_dependency_missing"
    PSRP_CONNECTION_FAILED = "psrp_connection_failed"


class DhcpEnvironmentError(Exception):
    """Runtime environment cannot support DHCP automation via PowerShell.

    Attributes:
        reason: Machine-readable code from DhcpEnvReason.
        detail: Human-readable explanation suitable for API responses.
    """

    def __init__(self, reason: str, detail: str) -> None:
        self.reason = reason
        self.detail = detail
        super().__init__(detail)


class PowerShellError(Exception):
    def __init__(
        self,
        command: str,
        stderr: str,
        returncode: int,
        *,
        operation: str | None = None,
        scope: str | None = None,
    ):
        self.command = command
        self.stderr = stderr
        self.returncode = returncode
        self.operation = operation
        self.scope = scope
        super().__init__(self.safe_message)

    @property
    def safe_stderr_preview(self) -> str:
        return sanitize_powershell_text(self.stderr)

    @property
    def safe_message(self) -> str:
        operation = self.operation or "unknown"
        return (
            f"PowerShell command failed "
            f"(operation={operation}, rc={self.returncode}): {self.safe_stderr_preview}"
        )

    def __str__(self) -> str:
        return self.safe_message


class PowerShellExecutionError(PowerShellError):
    """PowerShell exited non-zero or produced unusable output."""


class PowerShellTimeoutError(PowerShellError):
    """PowerShell process exceeded the configured timeout."""

    def __init__(
        self,
        command: str,
        timeout_seconds: int,
        *,
        operation: str | None = None,
        scope: str | None = None,
    ):
        self.timeout_seconds = timeout_seconds
        super().__init__(
            command,
            f"PowerShell command timed out after {timeout_seconds} seconds",
            -1,
            operation=operation,
            scope=scope,
        )


class ErrorCode:
    INVALID_SCOPE = "INVALID_SCOPE"
    UNAUTHORIZED = "UNAUTHORIZED"
    SCOPE_NOT_FOUND = "SCOPE_NOT_FOUND"
    DHCP_CONFLICT = "DHCP_CONFLICT"
    VALIDATION_ERROR = "VALIDATION_ERROR"
    POWERSHELL_COMMAND_FAILED = "POWERSHELL_COMMAND_FAILED"
    POWERSHELL_TIMEOUT = "POWERSHELL_TIMEOUT"
    DHCP_ENVIRONMENT_UNAVAILABLE = "DHCP_ENVIRONMENT_UNAVAILABLE"
    INTERNAL_ERROR = "INTERNAL_ERROR"
    BAD_REQUEST = "BAD_REQUEST"
    NOT_FOUND = "NOT_FOUND"
    METHOD_NOT_ALLOWED = "METHOD_NOT_ALLOWED"
    HTTP_ERROR = "HTTP_ERROR"


class AppError(Exception):
    """Base class for expected, client-safe application errors."""

    status_code = status.HTTP_500_INTERNAL_SERVER_ERROR
    code = ErrorCode.INTERNAL_ERROR
    message = "Internal server error"
    headers: dict[str, str] | None = None

    def __init__(
        self,
        message: str | None = None,
        *,
        details: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        if message is not None:
            self.message = message
        self.details = details or {}
        self.headers = headers
        super().__init__(self.message)


class InvalidScopeError(AppError):
    status_code = status.HTTP_400_BAD_REQUEST
    code = ErrorCode.INVALID_SCOPE

    def __init__(self, scope: str) -> None:
        super().__init__(
            f"Scope '{scope}' is not a valid IPv4 address",
            details={"scope": scope},
        )


class UnauthorizedError(AppError):
    status_code = status.HTTP_401_UNAUTHORIZED
    code = ErrorCode.UNAUTHORIZED

    def __init__(self) -> None:
        super().__init__(
            "Missing or invalid bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )


class ScopeNotFoundError(AppError):
    status_code = status.HTTP_404_NOT_FOUND
    code = ErrorCode.SCOPE_NOT_FOUND

    def __init__(self, scope: str) -> None:
        self.scope = scope
        super().__init__(
            f"DHCP scope {scope} was not found",
            details={"scope": scope},
        )


class DhcpConflictError(AppError):
    status_code = status.HTTP_409_CONFLICT
    code = ErrorCode.DHCP_CONFLICT

    def __init__(self, message: str = "DHCP state conflicts with the request") -> None:
        super().__init__(message)
