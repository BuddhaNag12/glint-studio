"""One error code per failure: every API error is JSON {"error": {"code", "message"}}."""

from __future__ import annotations

from typing import Literal

from fastapi import Request
from fastapi.responses import JSONResponse

ErrorCode = Literal[
    "engine_down",
    "model_missing",
    "workflow_failed",
    "bad_params",
    "unauthorized",
    "forbidden",
    "timeout",
    "provider_auth",
    "provider_quota",
    "provider_rejected",
    "provider_down",
]

ERROR_STATUS: dict[str, int] = {
    "engine_down": 503,
    "model_missing": 409,
    "workflow_failed": 500,
    "bad_params": 422,
    "unauthorized": 401,
    "forbidden": 403,
    "timeout": 504,
    "provider_auth": 502,
    "provider_quota": 429,
    "provider_rejected": 422,
    "provider_down": 503,
}


class ApiError(Exception):
    """A failure the client can act on. Clients switch on `code`, never on the status."""

    def __init__(self, code: ErrorCode, message: str) -> None:
        if code not in ERROR_STATUS:
            raise ValueError(f"Unknown error code '{code}'.")
        super().__init__(message)
        self.code = code
        self.message = message

    @property
    def status_code(self) -> int:
        return ERROR_STATUS[self.code]


async def api_error_handler(_request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, ApiError)
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": {"code": exc.code, "message": exc.message}},
    )
