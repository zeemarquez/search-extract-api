"""Error codes and the common error envelope.

Every error response has the shape ``{"error": {"code", "message", "details"}}``.
Clients should branch on ``code`` (stable) rather than on the HTTP status or message.
"""

from __future__ import annotations

import logging
from enum import StrEnum
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

logger = logging.getLogger(__name__)


class ErrorCode(StrEnum):
    # Client / API level
    UNAUTHORIZED = "UNAUTHORIZED"
    RATE_LIMITED = "RATE_LIMITED"
    VALIDATION_ERROR = "VALIDATION_ERROR"
    NOT_FOUND = "NOT_FOUND"
    INTERNAL_ERROR = "INTERNAL_ERROR"
    INVALID_URL = "INVALID_URL"
    BLOCKED_URL = "BLOCKED_URL"

    # Fetching / extraction
    FETCH_FAILED = "FETCH_FAILED"
    FETCH_TIMEOUT = "FETCH_TIMEOUT"
    UPSTREAM_HTTP_ERROR = "UPSTREAM_HTTP_ERROR"
    UPSTREAM_RATE_LIMITED = "UPSTREAM_RATE_LIMITED"
    CONTENT_TOO_LARGE = "CONTENT_TOO_LARGE"
    UNSUPPORTED_CONTENT = "UNSUPPORTED_CONTENT"
    EXTRACTION_FAILED = "EXTRACTION_FAILED"
    LOGIN_REQUIRED = "LOGIN_REQUIRED"
    CAPTCHA_REQUIRED = "CAPTCHA_REQUIRED"
    ACCESS_BLOCKED = "ACCESS_BLOCKED"
    DOMAIN_RATE_LIMITED = "DOMAIN_RATE_LIMITED"

    # Feeds
    FEED_NOT_FOUND = "FEED_NOT_FOUND"
    FEED_PARSE_ERROR = "FEED_PARSE_ERROR"

    # Sources (reported per source inside search responses)
    SOURCE_ERROR = "SOURCE_ERROR"
    SOURCE_TIMEOUT = "SOURCE_TIMEOUT"
    SOURCE_NOT_CONFIGURED = "SOURCE_NOT_CONFIGURED"
    SOURCE_UNKNOWN = "SOURCE_UNKNOWN"
    SOURCE_BLOCKED = "SOURCE_BLOCKED"
    PARSE_ERROR = "PARSE_ERROR"

    # Sessions
    SESSION_NOT_FOUND = "SESSION_NOT_FOUND"
    SESSION_INVALID = "SESSION_INVALID"
    SESSION_EXPIRED = "SESSION_EXPIRED"
    SESSION_REQUIRED = "SESSION_REQUIRED"
    SESSION_DOMAIN_MISMATCH = "SESSION_DOMAIN_MISMATCH"
    SESSION_RATE_LIMITED = "SESSION_RATE_LIMITED"
    LOGIN_NOT_FOUND = "LOGIN_NOT_FOUND"
    LOGIN_EXPIRED = "LOGIN_EXPIRED"
    LOGIN_INCOMPLETE = "LOGIN_INCOMPLETE"
    BROWSER_UNAVAILABLE = "BROWSER_UNAVAILABLE"

    # AI
    LLM_NOT_CONFIGURED = "LLM_NOT_CONFIGURED"
    LLM_UNAVAILABLE = "LLM_UNAVAILABLE"
    LLM_BAD_OUTPUT = "LLM_BAD_OUTPUT"


DEFAULT_STATUS: dict[ErrorCode, int] = {
    ErrorCode.UNAUTHORIZED: 401,
    ErrorCode.RATE_LIMITED: 429,
    ErrorCode.VALIDATION_ERROR: 422,
    ErrorCode.NOT_FOUND: 404,
    ErrorCode.INTERNAL_ERROR: 500,
    ErrorCode.INVALID_URL: 400,
    ErrorCode.BLOCKED_URL: 400,
    ErrorCode.FETCH_FAILED: 502,
    ErrorCode.FETCH_TIMEOUT: 504,
    ErrorCode.UPSTREAM_HTTP_ERROR: 502,
    ErrorCode.UPSTREAM_RATE_LIMITED: 502,
    ErrorCode.CONTENT_TOO_LARGE: 413,
    ErrorCode.UNSUPPORTED_CONTENT: 415,
    ErrorCode.EXTRACTION_FAILED: 422,
    # 409: the upstream site needs user action (log in again, solve a captcha...).
    ErrorCode.LOGIN_REQUIRED: 409,
    ErrorCode.CAPTCHA_REQUIRED: 409,
    ErrorCode.ACCESS_BLOCKED: 502,
    ErrorCode.DOMAIN_RATE_LIMITED: 429,
    ErrorCode.FEED_NOT_FOUND: 404,
    ErrorCode.FEED_PARSE_ERROR: 422,
    ErrorCode.SOURCE_ERROR: 502,
    ErrorCode.SOURCE_TIMEOUT: 504,
    ErrorCode.SOURCE_NOT_CONFIGURED: 400,
    ErrorCode.SOURCE_UNKNOWN: 400,
    ErrorCode.SOURCE_BLOCKED: 502,
    ErrorCode.PARSE_ERROR: 502,
    ErrorCode.SESSION_NOT_FOUND: 404,
    ErrorCode.SESSION_INVALID: 400,
    ErrorCode.SESSION_EXPIRED: 409,
    ErrorCode.SESSION_REQUIRED: 400,
    ErrorCode.SESSION_DOMAIN_MISMATCH: 400,
    ErrorCode.SESSION_RATE_LIMITED: 429,
    ErrorCode.LOGIN_NOT_FOUND: 404,
    ErrorCode.LOGIN_EXPIRED: 410,
    ErrorCode.LOGIN_INCOMPLETE: 409,
    ErrorCode.BROWSER_UNAVAILABLE: 503,
    ErrorCode.LLM_NOT_CONFIGURED: 503,
    ErrorCode.LLM_UNAVAILABLE: 503,
    ErrorCode.LLM_BAD_OUTPUT: 502,
}


class ApiError(Exception):
    """An error with a stable code, rendered as the common error envelope."""

    def __init__(
        self,
        code: ErrorCode,
        message: str,
        *,
        status_code: int | None = None,
        details: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = ErrorCode(code)
        self.message = message
        self.status_code = status_code or DEFAULT_STATUS.get(self.code, 500)
        self.details = details or {}
        self.headers = headers

    def to_dict(self) -> dict[str, Any]:
        return {"code": self.code.value, "message": self.message, "details": self.details}

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"ApiError({self.code.value}, {self.message!r})"


def error_body(code: ErrorCode | str, message: str, details: dict[str, Any] | None = None) -> dict:
    return {"error": {"code": str(code), "message": message, "details": details or {}}}


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(ApiError)
    async def _api_error(_: Request, exc: ApiError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content={"error": exc.to_dict()},
            headers=exc.headers,
        )

    @app.exception_handler(RequestValidationError)
    async def _validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
        errors = []
        for err in exc.errors():
            errors.append(
                {
                    "loc": list(err.get("loc", ())),
                    "msg": err.get("msg"),
                    "type": err.get("type"),
                }
            )
        return JSONResponse(
            status_code=422,
            content=error_body(ErrorCode.VALIDATION_ERROR, "Invalid request.", {"errors": errors}),
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(_: Request, exc: StarletteHTTPException) -> JSONResponse:
        code = ErrorCode.NOT_FOUND if exc.status_code == 404 else ErrorCode.INTERNAL_ERROR
        if exc.status_code == 401:
            code = ErrorCode.UNAUTHORIZED
        elif exc.status_code == 405:
            code = ErrorCode.VALIDATION_ERROR
        return JSONResponse(
            status_code=exc.status_code,
            content=error_body(code, str(exc.detail)),
            headers=getattr(exc, "headers", None),
        )

    @app.exception_handler(Exception)
    async def _unhandled(_: Request, exc: Exception) -> JSONResponse:
        logger.exception("Unhandled error: %s", type(exc).__name__)
        return JSONResponse(
            status_code=500,
            content=error_body(ErrorCode.INTERNAL_ERROR, "Internal server error."),
        )
