"""Settings page: Downloads (source, mirror, proxies) and Security (keyless generation on the LAN).

Read and changed only from this computer, with the main key, like the other settings.
"""

from __future__ import annotations

import asyncio

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
