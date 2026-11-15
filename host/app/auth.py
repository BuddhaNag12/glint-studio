"""API key authentication and signed image URL helpers."""

from __future__ import annotations

import hashlib
import hmac
import time

from fastapi import Header, HTTPException, Request, status

from app.errors import ApiError


def extract_api_token(authorization: str | None, x_api_key: str | None) -> str | None:
    """Extract a credential from either Authorization Bearer or X-API-Key headers."""
    if authorization:
        scheme, _, token = authorization.strip().partition(" ")
        if scheme.lower() == "bearer" and token.strip():
            return token.strip()
    if x_api_key and x_api_key.strip():
        return x_api_key.strip()
    return None


def verify_api_key(
    request: Request,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> None:
    """Validate incoming API requests using constant-time token comparison."""
    settings = request.app.state.settings
    if not settings.is_api_key_configured:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Server API_KEY is not configured. Set a strong API_KEY in .env.",
        )

    token = extract_api_token(authorization, x_api_key)
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing API key. Provide Authorization: Bearer <token> or X-API-Key.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    if not hmac.compare_digest(token.encode("utf-8"), settings.api_key.strip().encode("utf-8")):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Invalid API key.",
        )


def create_image_signature(filename: str, expires_at: int, secret: bytes) -> str:
    """Create an HMAC-SHA256 signature binding filename and expiration timestamp."""
    payload = f"{filename}:{expires_at}".encode("utf-8")
    return hmac.new(secret, payload, hashlib.sha256).hexdigest()


def build_signed_image_query(
    filename: str,
    ttl_seconds: int,
    secret: bytes,
    now: int | None = None,
) -> tuple[int, str]:
    """Return (expires_at, signature) for a generated image URL."""
    current_time = int(time.time()) if now is None else now
    expires_at = current_time + ttl_seconds
    signature = create_image_signature(filename, expires_at, secret)
    return expires_at, signature


def verify_image_signature(
    filename: str,
    exp: int,
    sig: str,
    secret: bytes,
    now: int | None = None,
) -> bool:
    """Verify expiration and HMAC signature for an image download request."""
    current_time = int(time.time()) if now is None else now
    if exp < current_time:
        return False
    expected = create_image_signature(filename, exp, secret)
    return hmac.compare_digest(expected, sig)


def require_api_key(
    request: Request,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> None:
    """verify_api_key for the newer endpoints: failures come back as {error:{code,message}}."""
    try:
        verify_api_key(request, authorization, x_api_key)
    except HTTPException as exc:
        code = {401: "unauthorized", 403: "forbidden"}.get(exc.status_code)
        if code is None:
            raise
        raise ApiError(code, str(exc.detail)) from exc
