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

from app.auth import require_main_key
from app.catalog import CatalogModel
from app.errors import ApiError
from app.keys import Key, KeyFields
from app.local_models import (
    Downloads,
    chosen_variant,
    load_hardware,
    measured_speed,
    memory_split,
    recommend,
    unlockable,
    variant_installed,
)
from app.model_folders import candidates, count_models, model_roots, normalize, read_settings, save_settings, update_settings
from app.stats import KEEP_DAYS, Stats, day_bounds

COOKIE = "glint_session"
LOGIN_TTL = 60
SESSION_MAX_AGE = 7 * 24 * 3600  # a copied cookie stops working after a week


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

    def valid(self, session: str | None, now: float | None = None) -> bool:
        issued, _, sig = (session or "").partition(".")
        if not (self.key and issued.isdigit() and sig) or not hmac.compare_digest(sig, self._sign(issued)):
            return False
        return 0 <= (time.time() if now is None else now) - int(issued) < SESSION_MAX_AGE


def local_only(request: Request) -> None:
    """The cookie pages and the sharing settings answer only to this computer: a site renamed to
    127.0.0.1 (DNS rebinding), a LAN address or the tunnel can't reach them. Tunnel requests come
    from cloudflared on 127.0.0.1, but uvicorn takes their address from its X-Forwarded-For."""
    allowed = request.app.state.settings.allowed_hosts
    host = request.headers.get("host", "").rsplit(":", 1)[0]
    client = request.client.host if request.client else ""
    if client not in ("127.0.0.1", "::1") or (allowed != ["*"] and host not in allowed):
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
    resp.set_cookie(COOKIE, session, max_age=SESSION_MAX_AGE, httponly=True, samesite="strict", path="/")
    return resp


@router.get("/", include_in_schema=False, dependencies=[Depends(local_only)])
async def dashboard_page(request: Request):
    page = _page(request)
    if not request.app.state.sessions.valid(request.cookies.get(COOKIE)):
        return HTMLResponse(GONE, status_code=403)
    return FileResponse(page, headers={"Cache-Control": "no-store"})


@router.get("/m", include_in_schema=False)
async def phone_page(request: Request):
    """The phone page, from any address: it holds no data, and asks for a key like any other client."""
    page = _page(request).with_name("phone.html")
    if not page.is_file():
        raise ApiError("not_found", "The phone page isn't installed with this host.")
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


def _roots(request: Request) -> list[Path]:
    cfg = request.app.state.settings
    return model_roots(cfg.models_dir, cfg.engine_file)


def _local_model(request: Request, model_id: str) -> CatalogModel:
    model = request.app.state.models.get(model_id)
    if model is None or not model.variants:
        raise ApiError("not_found", f"No downloadable model '{model_id}'.")
    return model


@router.get("/api/v1/local-models", dependencies=[Depends(require_main_key)])
async def local_models(request: Request) -> dict:
    """Every local model, best fit for this machine first, with download progress."""
    models_dir = _models_dir(request)
    hw = await asyncio.to_thread(load_hardware, request.app.state.settings.hardware_file, models_dir, request.app.state.settings.engine_file)
    downloads: Downloads = request.app.state.downloads
    roots = await asyncio.to_thread(_roots, request)
    rows = await asyncio.to_thread(
        recommend,
        hw,
        list(request.app.state.models.values()),
        lambda v: variant_installed(roots, v),
        lambda model_id: measured_speed(request.app.state.settings, model_id),
    )
    for row in rows:
        row["download"] = downloads.view(row["id"])
    return {"hardware": hw, "models": rows, "memory": memory_split(hw), "unlock": unlockable(hw, rows)}


@router.post("/api/v1/local-models/{model_id}/download", status_code=202, dependencies=[Depends(require_main_key)])
async def start_download(model_id: str, request: Request) -> dict:
    model = _local_model(request, model_id)
    hw = await asyncio.to_thread(load_hardware, request.app.state.settings.hardware_file, _models_dir(request), request.app.state.settings.engine_file)
    downloads: Downloads = request.app.state.downloads
    downloads.start(model.id, chosen_variant(hw, model))
    return downloads.view(model.id)


@router.get("/api/v1/local-models/{model_id}/download", dependencies=[Depends(require_main_key)])
async def download_status(model_id: str, request: Request) -> dict:
    view = request.app.state.downloads.view(_local_model(request, model_id).id)
    if view is None:
        raise ApiError("not_found", f"'{model_id}' isn't downloading.")
    return view


@router.delete("/api/v1/local-models/{model_id}/download", dependencies=[Depends(require_main_key)])
async def cancel_download(model_id: str, request: Request) -> dict:
    downloads: Downloads = request.app.state.downloads
    if downloads.model_id == _local_model(request, model_id).id:
        await downloads.cancel()
    return downloads.view(model_id) or {"status": "idle"}


@router.get("/api/v1/stats", dependencies=[Depends(require_main_key)])
async def stats(
    request: Request,
    days: int = Query(default=14, ge=1, le=KEEP_DAYS),
    range_: str | None = Query(default=None, alias="range"),
    from_: str | None = Query(default=None, alias="from"),
    to: str | None = None,
    tz: int = Query(default=0, ge=-840, le=840),  # minutes east of UTC; 0 keeps UTC days for old callers
    model: str | None = None,
    scope: str = Query(default="all", pattern="^(all|session)$"),
) -> dict:
    db: Stats = request.app.state.stats
    try:
        first, last = day_bounds(range_, days, from_, to, tz)
    except ValueError as exc:
        raise ApiError("bad_params", str(exc)) from None
    return db.summary(first, last, tz, model or None, scope == "session")


@router.delete("/api/v1/stats", status_code=204, dependencies=[Depends(require_main_key), Depends(local_only)])
async def clear_stats(request: Request) -> None:
    """Key daily caps keep counting; they live in their own table."""
    request.app.state.stats.clear()


class UsageSettings(BaseModel):
    record_usage: bool


@router.get("/api/v1/usage/settings", dependencies=[Depends(require_main_key), Depends(local_only)])
async def usage_settings(request: Request) -> dict:
    return {"record_usage": request.app.state.stats.recording(), "keep_days": KEEP_DAYS}


@router.put("/api/v1/usage/settings", dependencies=[Depends(require_main_key), Depends(local_only)])
async def set_usage_settings(body: UsageSettings, request: Request) -> dict:
    update_settings(_engine_file(request), {"record_usage": body.record_usage})
    return await usage_settings(request)


@router.get("/api/v1/system", dependencies=[Depends(require_main_key)])
async def system(request: Request) -> dict:
    """Live status: the engine's devices and queue, and the models folder."""
    models_dir = _models_dir(request)
    hw = await asyncio.to_thread(load_hardware, request.app.state.settings.hardware_file, models_dir, request.app.state.settings.engine_file)
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
        "engine": {"up": engine_up, "error": engine_error, "devices": devices, "model": request.app.state.engine.view()},
        "queue": {
            "running": sum(j.status == "running" for j in jobs),
            "queued": sum(j.status == "queued" for j in jobs),
        },
        "hardware": hw,
        "models_bytes": models_bytes,
    }


# --- the model in memory, and model folders linked from another ComfyUI ---

class LoadRequest(BaseModel):
    model: str | None = None  # None: the default model, or the first one downloaded


@router.post("/api/v1/engine/load", status_code=202, dependencies=[Depends(require_main_key)])
async def load_model(request: Request, body: LoadRequest | None = None) -> dict:
    engine = request.app.state.engine
    engine.load(body.model if body else None)
    return engine.view()


@router.post("/api/v1/engine/unload", dependencies=[Depends(require_main_key)])
async def unload_model(request: Request) -> dict:
    engine = request.app.state.engine
    await engine.unload()
    return engine.view()


class EngineSettings(BaseModel):
    preload: bool | None = None
    folders: list[str] | None = None


def _engine_file(request: Request) -> Path:
    path = request.app.state.settings.engine_file
    if path is None:
        raise ApiError("not_found", "Model folders are set up by the Glint Studio app; this host runs on its own.")
    return path


@router.get("/api/v1/engine/settings", dependencies=[Depends(require_main_key), Depends(local_only)])
async def engine_settings(request: Request) -> dict:
    """The preload switch, linked folders, and ComfyUI folders found on this computer."""
    current = read_settings(_engine_file(request))
    own = str((request.app.state.settings.models_dir or Path("models")).resolve())

    def view(f: str) -> dict:
        return {"path": f, "models": count_models(Path(f)), "exists": Path(f).is_dir()}

    def scan() -> dict:
        found = [f for f in candidates() if f != own and f not in current["folders"]]
        return {"preload": current["preload"], "folders": [view(f) for f in current["folders"]], "found": [view(f) for f in found]}

    return await asyncio.to_thread(scan)


@router.put("/api/v1/engine/settings", dependencies=[Depends(require_main_key), Depends(local_only)])
async def set_engine_settings(body: EngineSettings, request: Request) -> dict:
    """Changed only from this computer. New folders restart the engine, so not mid-job."""
    path = _engine_file(request)
    current = read_settings(path)
    folders = current["folders"]
    if body.folders is not None:
        folders = []
        for raw in body.folders:
            folder = await asyncio.to_thread(normalize, raw)
            if folder is None:
                raise ApiError("bad_params", f"There's no folder at {raw}.")
            if str(folder) not in folders:
                folders.append(str(folder))
        if folders != current["folders"] and any(
            j.status in ("queued", "running") for j in request.app.state.jobs.jobs.values()
        ):
            raise ApiError("bad_params", "Wait for the images in progress to finish; this restarts the engine.")
    preload = current["preload"] if body.preload is None else body.preload
    await asyncio.to_thread(save_settings, path, preload, folders)
    return await engine_settings(request)


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


def local_hostname() -> str | None:
    """This computer's Bonjour/mDNS name, <name>.local; whether it resolves depends on the network."""
    name = socket.gethostname().split(".")[0]
    return f"{name}.local" if name and name != "localhost" else None


def _network_file(request: Request) -> Path:
    path = request.app.state.settings.network_file
    if path is None:
        raise ApiError("not_found", "Sharing is set up by the Glint Studio app; this host runs on its own.")
    return path


@router.get("/api/v1/network", dependencies=[Depends(require_main_key)])
async def network(request: Request) -> dict:
    cfg = request.app.state.settings
    wanted = _read_json(_network_file(request))
    tunnel = _read_json(cfg.tunnel_file)
    address = lan_address()
    port = urlsplit(cfg.public_base_url).port
    return {
        "lan": {
            "on": bool(wanted.get("lan")),
            "active": cfg.serve_on_network,
            "url": f"http://{address}:{port}" if address and cfg.serve_on_network else None,
            "ip": address,  # shown even while sharing is off, so the Connect page can say it needs it
            "hostname": local_hostname(),
        },
        "tunnel": {
            "on": bool(wanted.get("tunnel")),
            "status": tunnel.get("status", "off"),
            "url": tunnel.get("url"),
            "error": tunnel.get("error"),
        },
    }


@router.put("/api/v1/network", dependencies=[Depends(require_main_key), Depends(local_only)])
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


# Making, changing and revoking keys only from this computer: a leaked main key used over the
# network can't plant a key that outlives rotating it.
@router.get("/api/v1/keys", dependencies=[Depends(require_main_key)])
async def list_keys(request: Request) -> dict:
    usage = request.app.state.stats.key_usage()
    return {"keys": [_key_view(k, usage) for k in request.app.state.keys.keys.values()]}


@router.post("/api/v1/keys", status_code=201, dependencies=[Depends(require_main_key), Depends(local_only)])
async def create_key(body: KeyFields, request: Request) -> dict:
    key, token = request.app.state.keys.create(body)
    return {**_key_view(key, {}), "key": token}  # the only time the key itself is shown


@router.put("/api/v1/keys/{key_id}", dependencies=[Depends(require_main_key), Depends(local_only)])
async def update_key(key_id: str, body: KeyFields, request: Request) -> dict:
    key = request.app.state.keys.update(key_id, body)
    if key is None:
        raise ApiError("not_found", f"No key '{key_id}'.")
    return _key_view(key, request.app.state.stats.key_usage())


@router.delete("/api/v1/keys/{key_id}", status_code=204, dependencies=[Depends(require_main_key), Depends(local_only)])
async def delete_key(key_id: str, request: Request) -> None:
    if not request.app.state.keys.delete(key_id):
        raise ApiError("not_found", f"No key '{key_id}'.")


def _key_view(key: Key, usage: dict) -> dict:
    made, last = usage.get(key.id, (0, None))
    return {**key.model_dump(exclude={"hash"}), "used_24h": made, "last_used": int(last) if last else None}


# --- the app's process logs, read-only; names from a fixed list, never a path from the request ---

LOG_FILES = {"host": "host.log", "comfyui": "comfyui.log", "tunnel": "tunnel.log"}
LOG_TAIL_BYTES = 512 * 1024


def _tail(path: Path, lines: int) -> list[str]:
    with path.open("rb") as f:
        size = f.seek(0, os.SEEK_END)
        f.seek(max(0, size - LOG_TAIL_BYTES))
        text = f.read().decode("utf-8", errors="replace")
    if size > LOG_TAIL_BYTES:
        text = text.split("\n", 1)[-1]  # drop the line the cut landed in
    return text.splitlines()[-lines:]


@router.get("/api/v1/logs", dependencies=[Depends(require_main_key), Depends(local_only)])
async def logs(
    request: Request, file: str = Query(default="host"), lines: int = Query(default=500, ge=1, le=5000)
) -> dict:
    """The end of one of the app's logs. Only from this computer: logs can hold prompts and addresses."""
    logs_dir: Path | None = request.app.state.settings.logs_dir
    if logs_dir is None:
        raise ApiError("not_found", "Logs are kept by the Glint Studio app; this host runs on its own.")
    if file not in LOG_FILES:
        raise ApiError("bad_params", f"No log '{file}'. Pick one of: {', '.join(LOG_FILES)}.")
    files = [name for name, fname in LOG_FILES.items() if (logs_dir / fname).is_file()]
    path = logs_dir / LOG_FILES[file]
    if not path.is_file():
        return {"files": files, "file": file, "lines": [], "total": 0}
    return {"files": files, "file": file, "lines": await asyncio.to_thread(_tail, path, lines),
            "total": await asyncio.to_thread(_count_lines, path)}


def _count_lines(path: Path) -> int:
    """Every line in the file, not just the tail sent, for "N of M lines"."""
    count, last = 0, b"\n"
    with path.open("rb") as f:
        while chunk := f.read(1 << 20):
            count, last = count + chunk.count(b"\n"), chunk[-1:]
    return count + (last != b"\n")  # a last line with no newline yet
