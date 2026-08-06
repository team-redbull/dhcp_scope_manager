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
    IMMUTABLE_FIELD = "IMMUTABLE_FIELD"
    VALIDATION_ERROR = "VALIDATION_ERROR"
    POWERSHELL_COMMAND_FAILED = "POWERSHELL_COMMAND_FAILED"
    POWERSHELL_TIMEOUT = "POWERSHELL_TIMEOUT"
    DHCP_ENVIRONMENT_UNAVAILABLE = "DHCP_ENVIRONMENT_UNAVAILABLE"
    TEST_TARGET_REFUSED = "TEST_TARGET_REFUSED"
    TEST_RUN_IN_PROGRESS = "TEST_RUN_IN_PROGRESS"
    TEST_RUN_NOT_FOUND = "TEST_RUN_NOT_FOUND"
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


class ImmutableScopeFieldError(AppError):
    """A field Windows cannot change in place differs from the observed state.

    Reported instead of attempting the write, because the alternative — deleting
    and recreating the scope — drops every active lease on that subnet and is
    not something a values-file edit should trigger silently.  Returning success
    without applying the change is worse still: the drift is then invisible and
    Crossplane re-sends the same PUT forever.
    """

    status_code = status.HTTP_409_CONFLICT
    code = ErrorCode.IMMUTABLE_FIELD

    def __init__(self, field: str, current: str, desired: str) -> None:
        self.field = field
        super().__init__(
            f"{field} cannot be changed on an existing scope: the DHCP server "
            f"holds {current}, the request asks for {desired}. Windows cannot "
            f"change it in place — the scope must be deleted and recreated.",
            details={"field": field, "current": current, "desired": desired},
        )


class TestTargetRefusedError(AppError):
    """The requested test target is a host this deployment must not test against.

    The live suite creates and deletes a real scope, so pointing it at a DHCP
    server that something depends on is destructive. Refused before any subprocess
    starts, rather than trusting whoever composed the request — a typo in a
    hostname is not a reason to write to production.
    """

    status_code = status.HTTP_422_UNPROCESSABLE_ENTITY
    code = ErrorCode.TEST_TARGET_REFUSED
    # Not a pytest test class — the name only starts with Test because it is
    # about test *runs*. Without this pytest tries to collect it.
    __test__ = False

    def __init__(self, host: str) -> None:
        self.host = host
        super().__init__(
            f"Refusing to run tests against '{host}': it is a protected DHCP "
            f"server for this deployment. The live suite creates and deletes "
            f"scopes, so it may only target a dedicated test server.",
            details={"host": host},
        )


class TestRunInProgressError(AppError):
    """One run at a time — concurrent suites would fight over the same test scope."""

    status_code = status.HTTP_409_CONFLICT
    code = ErrorCode.TEST_RUN_IN_PROGRESS
    # Not a pytest test class — the name only starts with Test because it is
    # about test *runs*. Without this pytest tries to collect it.
    __test__ = False

    def __init__(self, run_id: str) -> None:
        super().__init__(
            f"Test run {run_id} is still in progress. Only one run may execute at "
            f"a time: the suite owns a single scope and concurrent runs would "
            f"delete each other's state mid-test.",
            details={"runId": run_id},
        )


class TestRunNotFoundError(AppError):
    status_code = status.HTTP_404_NOT_FOUND
    code = ErrorCode.TEST_RUN_NOT_FOUND
    # Not a pytest test class — the name only starts with Test because it is
    # about test *runs*. Without this pytest tries to collect it.
    __test__ = False

    def __init__(self, run_id: str) -> None:
        super().__init__(
            f"No test run {run_id} on this instance. Runs are held in memory by "
            f"the pod that started them, so polling a different replica than the "
            f"one that accepted the POST will not find it.",
            details={"runId": run_id},
        )
