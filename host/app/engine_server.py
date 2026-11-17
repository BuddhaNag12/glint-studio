"""The engine as a server: idle unload, memory guard presets, restarts, and what the Overview's server card shows.

Settings live in engine.json. The desktop app reads the memory guard when it launches ComfyUI
(comfyui_spec in runtime.rs), and restarts ComfyUI when the host leaves an `engine_restart` file
beside engine.json, the same way it applies network.json.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field

from app.auth import require_main_key
from app.dashboard import local_only
from app.errors import ApiError
from app.model_folders import get_setting, update_settings

MAX_IDLE_MINUTES = 7 * 24 * 60
MAX_RESERVE_GB = 128.0
SAFE_RESERVE_GB = 2  # mirrors SAFE_RESERVE_GB in runtime.rs
GUARDS = ("safe", "balanced", "aggressive", "custom")
STARTING_FOR = 180  # seconds a fresh host waits for ComfyUI before calling it stopped (COMFYUI_START_TIMEOUT)


def _minutes(value) -> int | None:
    ok = isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= MAX_IDLE_MINUTES
    return value if ok else None


def idle_minutes(engine_file: Path | None, model_id: str) -> int:
    """The model's own limit if it has one, else the global one; 0 = never."""
    by_model = get_setting(engine_file, "idle_unload_by_model", {})
    own = _minutes(by_model.get(model_id)) if isinstance(by_model, dict) else None
    return own if own is not None else (_minutes(get_setting(engine_file, "idle_unload_minutes", 0)) or 0)


def read_server_settings(engine_file: Path | None) -> dict:
    guard = get_setting(engine_file, "memory_guard", "balanced")
    reserve = get_setting(engine_file, "reserve_vram_gb", SAFE_RESERVE_GB)
    by_model = get_setting(engine_file, "idle_unload_by_model", {})
    return {
        "idle_unload_minutes": _minutes(get_setting(engine_file, "idle_unload_minutes", 0)) or 0,
        "idle_unload_by_model": {k: v for k, v in by_model.items() if _minutes(v) is not None} if isinstance(by_model, dict) else {},
        "memory_guard": guard if guard in GUARDS else "balanced",
        "reserve_vram_gb": reserve
        if isinstance(reserve, (int, float)) and not isinstance(reserve, bool) and 0 <= reserve <= MAX_RESERVE_GB
        else SAFE_RESERVE_GB,
    }


def memory_flags(guard: str, reserve_gb: float) -> list[str]:
    """ComfyUI launch flags for a preset; mirrors memory_flags() in runtime.rs."""
    if guard == "safe":
        return ["--lowvram", "--reserve-vram", str(SAFE_RESERVE_GB)]
    if guard == "aggressive":
        return ["--highvram"]
    if guard == "custom":
        return ["--reserve-vram", str(reserve_gb)]
    return []


def memory_args(argv: list[str]) -> tuple[frozenset[str], float | None]:
    """The memory flags in a ComfyUI command line, comparable whatever their order or number format."""
    modes = frozenset(a for a in argv if a in ("--lowvram", "--highvram", "--novram", "--gpu-only"))
    reserve = None
    if "--reserve-vram" in argv:
        i = argv.index("--reserve-vram")
        try:
            reserve = float(argv[i + 1])
        except (IndexError, ValueError):
            pass
    return modes, reserve


def restart_needed(settings: dict, argv: list[str] | None) -> bool:
    """ComfyUI runs with other memory flags than the settings ask for. Unknown (engine down) is False."""
    if not isinstance(argv, list):
        return False
    want = memory_flags(settings["memory_guard"], settings["reserve_vram_gb"])
    return memory_args(want) != memory_args([str(a) for a in argv])


def custom_nodes(comfyui_dir: Path | None) -> list[str]:
    folder = comfyui_dir / "custom_nodes" if comfyui_dir else None
    try:
        return sorted(p.name for p in folder.iterdir() if p.is_dir() and not p.name.startswith((".", "__"))) if folder else []
    except OSError:
        return []


router = APIRouter()


def _engine_file(request: Request) -> Path:
    path = request.app.state.settings.engine_file
    if path is None:
        raise ApiError("not_found", "Engine settings are kept by the Glint Studio app; this host runs on its own.")
    return path


def _busy(request: Request) -> bool:
    return any(j.status in ("queued", "running") for j in request.app.state.jobs.jobs.values())


@router.get("/api/v1/engine/server", dependencies=[Depends(require_main_key)])
async def server(request: Request) -> dict:
    """The engine server's state for the Overview card and the Settings page."""
    cfg = request.app.state.settings
    engine = request.app.state.engine
    try:
        stats = await request.app.state.providers["comfyui"].system_stats()
    except ApiError:
        stats = None
    system = (stats or {}).get("system") or {}
    restarting = bool(cfg.engine_file and cfg.engine_file.with_name("engine_restart").exists())
    if stats is not None:
        status = "running"
    elif restarting or time.time() - engine.started < STARTING_FOR:
        status = "starting"
    else:
        status = "stopped"
    settings = read_server_settings(cfg.engine_file)
    has_model = await asyncio.to_thread(lambda: engine.pick() is not None)
    return {
        "status": status,
        "address": cfg.public_base_url,
        "up_since": engine.up_since,
        "has_model": has_model,
        "settings": settings,
        "restart_needed": not restarting and restart_needed(settings, system.get("argv")),
        "versions": {
            "comfyui": system.get("comfyui_version"),
            "python": str(system.get("python_version") or "").split(" ")[0] or None,
            "torch": system.get("pytorch_version"),
            "custom_nodes": await asyncio.to_thread(custom_nodes, cfg.comfyui_dir),
        }
        if stats is not None
        else None,
    }


class ServerSettings(BaseModel):
    idle_unload_minutes: int | None = Field(default=None, ge=0, le=MAX_IDLE_MINUTES)
    idle_unload_by_model: dict[str, int] | None = None
    memory_guard: Literal["safe", "balanced", "aggressive", "custom"] | None = None
    reserve_vram_gb: float | None = Field(default=None, ge=0, le=MAX_RESERVE_GB)


@router.put("/api/v1/engine/server", dependencies=[Depends(require_main_key), Depends(local_only)])
async def set_server(body: ServerSettings, request: Request) -> dict:
    """Idle limits apply at once; a new memory guard waits for the next engine restart."""
    path = _engine_file(request)
    patch = body.model_dump(exclude_none=True)
    for model_id, minutes in (body.idle_unload_by_model or {}).items():
        if model_id not in request.app.state.models or _minutes(minutes) is None:
            raise ApiError("bad_params", f"idle_unload_by_model: '{model_id}' needs a known model and 0–{MAX_IDLE_MINUTES} minutes.")
    await asyncio.to_thread(update_settings, path, patch)
    return await server(request)


@router.post("/api/v1/engine/restart", status_code=202, dependencies=[Depends(require_main_key), Depends(local_only)])
async def restart(request: Request) -> dict:
    """Asks the app to restart ComfyUI (it reads the memory guard then). Not mid-job."""
    path = _engine_file(request)
    if _busy(request):
        raise ApiError("bad_params", "Wait for the images in progress to finish; this restarts the engine.")
    await asyncio.to_thread(path.with_name("engine_restart").write_text, str(int(time.time())))
    return await server(request)
