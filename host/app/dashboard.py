"""The web dashboard: login from the menu-bar app, the page itself, and the data it shows.

The desktop app opens `/login?exp=&sig=`, signed with the API key and good once for 60 seconds.
That sets an HttpOnly, SameSite=Strict session cookie, and the page trades the cookie for the
URL and API key (`/api/v1/dashboard/connect`) to call the rest of the API like any client.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import socket
import time
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from pydantic import BaseModel

from app.auth import require_api_key
from app.catalog import CatalogModel
from app.errors import ApiError
from app.local_models import (
    Downloads,
    chosen_variant,
    load_hardware,
    recommend,
    variant_installed,
)
from app.stats import Stats

COOKIE = "glint_session"
LOGIN_TTL = 60


def login_signature(api_key: str, exp: int) -> str:
    return hmac.new(api_key.encode(), f"glint-dashboard-login:{exp}".encode(), hashlib.sha256).hexdigest()


class Sessions:
    """A session is its start time signed with the API key, so it outlives a host restart (turning
    network sharing on restarts the host). Rotating the key ends every session."""

    def __init__(self, api_key: str) -> None:
        self.key = api_key.encode()
        self.used: dict[str, int] = {}  # login signatures already spent, until they expire

    def _sign(self, issued: str) -> str:
        return hmac.new(self.key, f"glint-dashboard-session:{issued}".encode(), hashlib.sha256).hexdigest()

    def login(self, exp: int, sig: str, now: float | None = None) -> str | None:
        now = time.time() if now is None else now
        self.used = {s: e for s, e in self.used.items() if e >= now}
        if not self.key or exp < now or exp > now + LOGIN_TTL or sig in self.used:
            return None
        if not hmac.compare_digest(sig, login_signature(self.key.decode(), exp)):
            return None
        self.used[sig] = exp
        issued = str(int(now))
        return f"{issued}.{self._sign(issued)}"

    def valid(self, session: str | None) -> bool:
        issued, _, sig = (session or "").partition(".")
        return bool(self.key and issued and sig) and hmac.compare_digest(sig, self._sign(issued))


def local_only(request: Request) -> None:
    """The cookie pages answer only to this computer's names: a site renamed to 127.0.0.1 (DNS
    rebinding), a LAN address or the tunnel can't reach them."""
    allowed = request.app.state.settings.allowed_hosts
    host = request.headers.get("host", "").rsplit(":", 1)[0]
    if allowed != ["*"] and host not in allowed:
        raise HTTPException(400, "Invalid host header")


GONE = """<!doctype html><meta charset="utf-8"><title>Glint Studio</title>
<body style="font:16px system-ui;margin:4rem auto;max-width:32rem;padding:0 1rem">
<h1>This link has expired</h1><p>Open the dashboard from the Glint Studio menu again.</p>"""

router = APIRouter()


def _page(request: Request) -> Path:
    page_dir = request.app.state.settings.dashboard_dir
    if page_dir is None or not (page_dir / "dashboard.html").is_file():
        raise ApiError("not_found", "The dashboard isn't installed with this host.")
    return page_dir / "dashboard.html"


@router.get("/login", include_in_schema=False, dependencies=[Depends(local_only)])
async def login(request: Request, exp: int = Query(...), sig: str = Query(...)):
    session = request.app.state.sessions.login(exp, sig)
    if session is None:
        return HTMLResponse(GONE, status_code=403)
    resp = RedirectResponse("/", status_code=303)
    resp.set_cookie(COOKIE, session, httponly=True, samesite="strict", path="/")
    return resp


@router.get("/", include_in_schema=False, dependencies=[Depends(local_only)])
async def dashboard_page(request: Request):
    page = _page(request)
    if not request.app.state.sessions.valid(request.cookies.get(COOKIE)):
        return HTMLResponse(GONE, status_code=403)
    return FileResponse(page, headers={"Cache-Control": "no-store"})


@router.get("/assets/{name}", include_in_schema=False)
async def dashboard_asset(name: str, request: Request):
    assets = (_page(request).parent / "assets").resolve()
    path = (assets / name).resolve()
    if not path.is_relative_to(assets) or not path.is_file():
        raise ApiError("not_found", "No such file.")
    return FileResponse(path, headers={"Cache-Control": "public, max-age=31536000, immutable"})


@router.get("/api/v1/dashboard/connect", include_in_schema=False, dependencies=[Depends(local_only)])
async def connect(request: Request) -> dict:
    """The cookie in, the API key out: same-site only, and only under this computer's names."""
    if not request.app.state.sessions.valid(request.cookies.get(COOKIE)):
        raise ApiError("unauthorized", "Open the dashboard from the Glint Studio menu.")
    cfg = request.app.state.settings
    return {"url": cfg.public_base_url, "api_key": cfg.api_key.strip()}


# --- data the dashboard shows; each needs the API key like the rest of the API ---

def _models_dir(request: Request) -> Path:
    return request.app.state.settings.models_dir or Path("models")


def _local_model(request: Request, model_id: str) -> CatalogModel:
    model = request.app.state.models.get(model_id)
    if model is None or not model.variants:
        raise ApiError("not_found", f"No downloadable model '{model_id}'.")
    return model


@router.get("/api/v1/local-models", dependencies=[Depends(require_api_key)])
async def local_models(request: Request) -> dict:
    """Every local model, best fit for this machine first, with download progress."""
    models_dir = _models_dir(request)
    hw = await asyncio.to_thread(load_hardware, request.app.state.settings.hardware_file, models_dir)
    downloads: Downloads = request.app.state.downloads
    rows = recommend(hw, list(request.app.state.models.values()), lambda v: variant_installed(models_dir, v))
    for row in rows:
        row["download"] = downloads.view(row["id"])
    return {"hardware": hw, "models": rows}


@router.post("/api/v1/local-models/{model_id}/download", status_code=202, dependencies=[Depends(require_api_key)])
async def start_download(model_id: str, request: Request) -> dict:
    model = _local_model(request, model_id)
    hw = await asyncio.to_thread(load_hardware, request.app.state.settings.hardware_file, _models_dir(request))
    downloads: Downloads = request.app.state.downloads
    downloads.start(model.id, chosen_variant(hw, model))
    return downloads.view(model.id)


@router.get("/api/v1/local-models/{model_id}/download", dependencies=[Depends(require_api_key)])
async def download_status(model_id: str, request: Request) -> dict:
    view = request.app.state.downloads.view(_local_model(request, model_id).id)
    if view is None:
        raise ApiError("not_found", f"'{model_id}' isn't downloading.")
    return view


@router.delete("/api/v1/local-models/{model_id}/download", dependencies=[Depends(require_api_key)])
async def cancel_download(model_id: str, request: Request) -> dict:
    downloads: Downloads = request.app.state.downloads
    if downloads.model_id == _local_model(request, model_id).id:
        await downloads.cancel()
    return downloads.view(model_id) or {"status": "idle"}


@router.get("/api/v1/stats", dependencies=[Depends(require_api_key)])
async def stats(request: Request, days: int = Query(default=14, ge=1, le=90)) -> dict:
    db: Stats = request.app.state.stats
    return db.summary(days)


@router.get("/api/v1/system", dependencies=[Depends(require_api_key)])
async def system(request: Request) -> dict:
    """Live status: the engine's devices and queue, and the models folder."""
    models_dir = _models_dir(request)
    hw = await asyncio.to_thread(load_hardware, request.app.state.settings.hardware_file, models_dir)
    jobs = request.app.state.jobs.jobs.values()
    try:
        engine = await request.app.state.providers["comfyui"].system_stats()
        devices = [
            {"name": d.get("name"), "type": d.get("type"), "vram_total": d.get("vram_total"), "vram_free": d.get("vram_free")}
            for d in engine.get("devices", [])
        ]
        engine_up, engine_error = True, None
    except ApiError as exc:
        devices, engine_up, engine_error = [], False, exc.message
    models_bytes = await asyncio.to_thread(
        lambda: sum(p.stat().st_size for p in models_dir.rglob("*") if p.is_file())
    )
    return {
        "engine": {"up": engine_up, "error": engine_error, "devices": devices},
        "queue": {
            "running": sum(j.status == "running" for j in jobs),
            "queued": sum(j.status == "queued" for j in jobs),
        },
        "hardware": hw,
        "models_bytes": models_bytes,
    }


# --- sharing on the network and the public link; the desktop app does the work ---

class NetworkRequest(BaseModel):
    lan: bool | None = None
    tunnel: bool | None = None


def _read_json(path: Path | None) -> dict:
    try:
        data = json.loads(path.read_text()) if path else {}
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def lan_address() -> str | None:
    """This computer's address on the network it reaches the internet through."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        try:
            s.connect(("192.0.2.1", 9))  # UDP: picks the route, sends nothing
            return s.getsockname()[0]
        except OSError:
            return None


def _network_file(request: Request) -> Path:
    path = request.app.state.settings.network_file
    if path is None:
        raise ApiError("not_found", "Sharing is set up by the Glint Studio app; this host runs on its own.")
    return path


@router.get("/api/v1/network", dependencies=[Depends(require_api_key)])
async def network(request: Request) -> dict:
    cfg = request.app.state.settings
    wanted = _read_json(_network_file(request))
    tunnel = _read_json(cfg.tunnel_file)
    address = lan_address() if cfg.serve_on_network else None
    port = urlsplit(cfg.public_base_url).port
    return {
        "lan": {
            "on": bool(wanted.get("lan")),
            "active": cfg.serve_on_network,
            "url": f"http://{address}:{port}" if address else None,
        },
        "tunnel": {
            "on": bool(wanted.get("tunnel")),
            "status": tunnel.get("status", "off"),
            "url": tunnel.get("url"),
            "error": tunnel.get("error"),
        },
    }


@router.put("/api/v1/network", dependencies=[Depends(require_api_key), Depends(local_only)])
async def set_network(body: NetworkRequest, request: Request) -> dict:
    """Changed only from this computer. Network sharing restarts the host, so not mid-job."""
    path = _network_file(request)
    wanted = {"lan": False, "tunnel": False, **_read_json(path)}
    if body.lan is not None and body.lan != wanted["lan"]:
        if any(j.status in ("queued", "running") for j in request.app.state.jobs.jobs.values()):
            raise ApiError("bad_params", "Wait for the images in progress to finish; this restarts the host.")
        wanted["lan"] = body.lan
    if body.tunnel is not None:
        wanted["tunnel"] = body.tunnel
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(wanted))
    os.replace(tmp, path)  # the app never reads half a file
    return await network(request)
