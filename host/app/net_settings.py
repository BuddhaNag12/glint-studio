"""Settings page: Downloads (source, mirror, proxies), Security (keyless generation on the LAN) and Erase.

Read and changed only from this computer, with the main key, like the other settings.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, StrictBool

from app import net
from app.auth import require_main_key
from app.dashboard import _engine_file, local_only
from app.errors import ApiError
from app.model_folders import get_setting, update_settings

router = APIRouter(prefix="/api/v1/settings", dependencies=[Depends(require_main_key), Depends(local_only)])


@router.get("/downloads")
async def downloads(request: Request) -> dict:
    """The saved values, and what's wrong with them (a CA file deleted since), if anything."""
    path = _engine_file(request)
    try:
        return {**(await asyncio.to_thread(net.read, path)).model_dump(), "error": None}
    except ApiError as exc:
        raw = get_setting(path, "downloads", {})
        return {**net.DEFAULT.model_dump(), **{k: raw[k] for k in net.DEFAULT.model_dump() if k in raw}, "error": exc.message}


@router.put("/downloads")
async def set_downloads(body: net.DownloadSettings, request: Request) -> dict:
    """Whole, as the page sends it; FastAPI has already checked the addresses and the CA file."""
    await asyncio.to_thread(update_settings, _engine_file(request), {"downloads": body.model_dump()})
    return await downloads(request)


class Security(BaseModel):
    lan_open_generation: StrictBool


@router.get("/security")
async def security(request: Request) -> dict:
    return {
        "lan_open_generation": get_setting(_engine_file(request), "lan_open_generation", False) is True,
        "lan_active": request.app.state.settings.serve_on_network,
    }


@router.put("/security")
async def set_security(body: Security, request: Request) -> dict:
    await asyncio.to_thread(update_settings, _engine_file(request), {"lan_open_generation": body.lan_open_generation})
    return await security(request)


# --- Erase all data and quit ---
# The app-data folder is engine.json's folder. The app watches for ERASE_REQUEST there, then stops this
# host and ComfyUI, deletes the folder and its keychain entries, and quits.
ERASE_REQUEST = "erase_requested"


def _size(root: Path) -> int:
    """Bytes under root. Links aren't followed: folders linked from another ComfyUI aren't ours to count."""
    total = 0
    for folder, _, files in os.walk(root):
        for name in files:
            try:
                total += os.lstat(os.path.join(folder, name)).st_size
            except OSError:
                pass  # deleted while we walked
    return total


@router.get("/erase")
async def erase_info(request: Request) -> dict:
    """What Erase would free, for the confirm."""
    path = _engine_file(request)
    models = request.app.state.settings.models_dir
    total, models_bytes = await asyncio.to_thread(lambda: (_size(path.parent), _size(models) if models else 0))
    linked = get_setting(path, "folders", [])
    return {
        "folder": str(path.parent),
        "total_bytes": total,
        "models_bytes": models_bytes,
        "linked_folders": len(linked) if isinstance(linked, list) else 0,
    }


@router.post("/erase", status_code=202)
async def erase(request: Request) -> dict:
    await asyncio.to_thread((_engine_file(request).parent / ERASE_REQUEST).touch)
    return {"erasing": True}
