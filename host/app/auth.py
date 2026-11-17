"""API key authentication and signed image URL helpers."""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import time
from collections.abc import Mapping

from fastapi import Depends, Header, HTTPException, Request, status

from app.errors import ApiError
from app.keys import Key
from app.model_folders import get_setting


def extract_api_token(authorization: str | None, x_api_key: str | None) -> str | None:
    """Extract a credential from either Authorization Bearer or X-API-Key headers."""
    if authorization:
        scheme, _, token = authorization.strip().partition(" ")
        if scheme.lower() == "bearer" and token.strip():
            return token.strip()
    if x_api_key and x_api_key.strip():
        return x_api_key.strip()
    return None


WS_PROTOCOL = "glint.v1"
WS_KEY_PREFIX = "glint.key."


def ws_protocol_key(headers: Mapping[str, str]) -> str | None:
    """The key a browser offers as a subprotocol, ["glint.v1", "glint.key.<key>"], so it stays out of URLs and logs."""
    for proto in (headers.get("sec-websocket-protocol") or "").split(","):
        if proto.strip().startswith(WS_KEY_PREFIX):
            return proto.strip()[len(WS_KEY_PREFIX):]
    return None


def ws_api_key(app, headers: Mapping[str, str], query_key: str | None = None) -> Key | None:
    """For WebSockets, where browsers can't set headers: the headers, a subprotocol, or an ?api_key= query."""
    token = extract_api_token(headers.get("authorization"), headers.get("x-api-key")) or ws_protocol_key(headers) or query_key
    return app.state.keys.find(token) if app.state.settings.is_api_key_configured else None


# "Allow image generation without a key from your local network" (engine.json lan_open_generation):
# such requests act as this key, which makes images with local models only, and only on these routes.
LAN_GUEST = Key(id="lan", name="Local network, no key", scopes=["local"])
LAN_OPEN_ROUTES = {
    ("POST", "/v1/images/generations"),
    ("POST", "/api/v1/jobs"),
    ("GET", "/api/v1/jobs/{job_id}"),
    ("DELETE", "/api/v1/jobs/{job_id}"),
    ("GET", "/api/v1/models"),
    ("GET", "/api/v1/styles"),
}
LAN_NETWORKS = [ipaddress.ip_network(n) for n in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "169.254.0.0/16", "fc00::/7", "fe80::/10")]
# Any of these means a proxy passed the request on. The tunnel's cloudflared runs on 127.0.0.1 and
# uvicorn swaps in the X-Forwarded-For address, so a forwarded request never counts as the LAN.
PROXY_HEADERS = ("x-forwarded-for", "forwarded", "x-real-ip", "cf-connecting-ip", "cf-ray", "true-client-ip", "x-forwarded-host")


def direct_lan_request(request: Request) -> bool:
    """From a private LAN address, straight to this host, under an address-like Host name."""
    if any(h in request.headers for h in PROXY_HEADERS):
        return False
    try:
        ip = ipaddress.ip_address(request.client.host if request.client else "")
    except ValueError:
        return False
    ip = getattr(ip, "ipv4_mapped", None) or ip
    if not any(ip in net for net in LAN_NETWORKS):
        return False
    # A site renamed to this address (DNS rebinding) sends its own name; a LAN client sends the IP or .local name.
    host = request.url.hostname or ""
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return host.endswith(".local")


def lan_open(request: Request) -> bool:
    cfg = request.app.state.settings
    route = request.scope.get("route")
    return (
        cfg.serve_on_network
        and (request.method, getattr(route, "path", None)) in LAN_OPEN_ROUTES
        and direct_lan_request(request)
        and get_setting(cfg.engine_file, "lan_open_generation", False) is True
    )


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
    if not token and lan_open(request):
        request.state.key = LAN_GUEST
        return
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing API key. Provide Authorization: Bearer <token> or X-API-Key.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    key = request.app.state.keys.find(token)
    if key is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Invalid API key.",
        )
    request.state.key = key


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


def require_main_key(request: Request, _: None = Depends(require_api_key)) -> None:
    """Settings and downloads: only the main key. Extra keys can make images and nothing else."""
    if not request.state.key.owner:
        raise ApiError("forbidden", "This key can only make images. Changing settings needs the main key.")
