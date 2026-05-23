from __future__ import annotations

import logging
from typing import Any

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.errors import AppError, DhcpEnvironmentError, DhcpEnvReason, ErrorCode, PowerShellError, PowerShellTimeoutError, sanitize_powershell_text
from app.services.ps_executor import is_already_exists_error

logger = logging.getLogger(__name__)

_MAX_PS_ERROR_LEN = 500


def _sanitize_text(value: str, *, max_len: int = _MAX_PS_ERROR_LEN) -> str:
    """Remove high-risk infrastructure details before returning text to clients."""
    return sanitize_powershell_text(value, max_len=max_len)


def _error_content(
    code: str,
    message: str,
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "error": {
            "code": code,
            "message": message,
            "details": details or {},
        }
    }


def _error_response(
    status_code: int,
    code: str,
    message: str,
    details: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content=_error_content(code=code, message=message, details=details),
        headers=headers,
    )


def _validation_field(loc: tuple[Any, ...] | list[Any]) -> str:
    return ".".join(str(part) for part in loc)


def _validation_errors(exc: RequestValidationError) -> list[dict[str, str]]:
    return [
        {
            "field": _validation_field(error.get("loc", ())),
            "message": str(error.get("msg", "Invalid value")),
            "type": str(error.get("type", "value_error")),
        }
        for error in exc.errors()
    ]


def _http_error_code(status_code: int) -> str:
    return {
        status.HTTP_400_BAD_REQUEST: ErrorCode.BAD_REQUEST,
        status.HTTP_401_UNAUTHORIZED: ErrorCode.UNAUTHORIZED,
        status.HTTP_404_NOT_FOUND: ErrorCode.NOT_FOUND,
        status.HTTP_405_METHOD_NOT_ALLOWED: ErrorCode.METHOD_NOT_ALLOWED,
        status.HTTP_422_UNPROCESSABLE_ENTITY: ErrorCode.VALIDATION_ERROR,
    }.get(status_code, ErrorCode.HTTP_ERROR)


def _http_error_message(exc: StarletteHTTPException) -> str:
    if isinstance(exc.detail, str):
        return exc.detail
    return "HTTP error"


def _dhcp_env_message(reason: str) -> str:
    return {
        DhcpEnvReason.UNSUPPORTED_OS: "Backend host is not a supported DHCP automation runtime",
        DhcpEnvReason.WSL_DETECTED: "Backend is running in WSL and cannot perform DHCP automation",
        DhcpEnvReason.POWERSHELL_NOT_FOUND: "Windows PowerShell is not available",
        DhcpEnvReason.POWERSHELL_EXEC_FAILED: "Windows PowerShell failed its startup check",
        DhcpEnvReason.DHCP_CMDLETS_UNAVAILABLE: "DHCP PowerShell cmdlets are unavailable",
    }.get(reason, "DHCP automation runtime is unavailable")



def register_exception_handlers(app: FastAPI) -> None:
    """Register global HTTP translations for all expected project errors."""

    @app.exception_handler(AppError)
    async def app_error_handler(request: Request, exc: AppError) -> JSONResponse:
        logger.info(
            "Application error on %s %s [%s]: %s",
            request.method,
            request.url.path,
            exc.code,
            exc.message,
        )
        return _error_response(
            status_code=exc.status_code,
            code=exc.code,
            message=exc.message,
            details=exc.details,
            headers=exc.headers,
        )

    @app.exception_handler(DhcpEnvironmentError)
    async def dhcp_env_error_handler(
        request: Request, exc: DhcpEnvironmentError
    ) -> JSONResponse:
        logger.error(
            "DHCP environment error on %s %s [%s]: %s",
            request.method,
            request.url.path,
            exc.reason,
            exc.detail,
        )
        return _error_response(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            code=ErrorCode.DHCP_ENVIRONMENT_UNAVAILABLE,
            message=_dhcp_env_message(exc.reason),
            details={"reason": exc.reason},
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        errors = _validation_errors(exc)
        logger.info(
            "Request validation failed on %s %s: %s",
            request.method,
            request.url.path,
            errors,
        )
        return _error_response(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            code=ErrorCode.VALIDATION_ERROR,
            message="Request validation failed",
            details={"errors": errors},
        )

    @app.exception_handler(StarletteHTTPException)
    async def http_exception_handler(
        request: Request, exc: StarletteHTTPException
    ) -> JSONResponse:
        logger.info(
            "HTTP error on %s %s [%s]: %s",
            request.method,
            request.url.path,
            exc.status_code,
            exc.detail,
        )
        return _error_response(
            status_code=exc.status_code,
            code=_http_error_code(exc.status_code),
            message=_http_error_message(exc),
            headers=getattr(exc, "headers", None),
        )

    @app.exception_handler(PowerShellError)
    async def powershell_error_handler(request: Request, exc: PowerShellError) -> JSONResponse:
        safe_stderr = _sanitize_text(exc.stderr)
        logger.error(
            "PowerShell error on %s %s",
            request.method,
            request.url.path,
            extra={
                "scope_id": exc.scope_id,
                "operation": exc.operation or "powershell",
                "returncode": exc.returncode,
                "status": "failed",
                "error_code": ErrorCode.POWERSHELL_COMMAND_FAILED,
                "stderr_preview": safe_stderr,
            },
            exc_info=True,
        )

        if isinstance(exc, PowerShellTimeoutError) or exc.returncode == -1:
            return _error_response(
                status_code=status.HTTP_504_GATEWAY_TIMEOUT,
                code=ErrorCode.POWERSHELL_TIMEOUT,
                message="Timed out while waiting for DHCP PowerShell command to finish",
            )

        if is_already_exists_error(exc.stderr):
            return _error_response(
                status_code=status.HTTP_409_CONFLICT,
                code=ErrorCode.DHCP_CONFLICT,
                message="DHCP state conflicts with the requested operation",
            )

        return _error_response(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            code=ErrorCode.POWERSHELL_COMMAND_FAILED,
            message="Failed to apply DHCP scope configuration",
        )

    @app.exception_handler(Exception)
    async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
        logger.exception(
            "Unhandled error on %s %s",
            request.method,
            request.url.path,
        )
        return _error_response(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            code=ErrorCode.INTERNAL_ERROR,
            message="Internal server error",
        )
