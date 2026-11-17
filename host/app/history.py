"""Past images for the Generate page, and its saved settings profiles.

Each finished image is kept on disk next to a small JSON sidecar (prompt, model, style, size, steps,
seed, time), so it outlives a host restart and the browser it was made in. The outputs folder isn't
used: it's a cache for image links, emptied after their TTL.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import re
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, Field, ValidationError

from app.auth import require_api_key, require_main_key
from app.errors import ApiError
from app.jobs import Job
from app.model_folders import get_setting, update_settings

logger = logging.getLogger(__name__)

# shortcut: the newest 500 images (about 1 GB of 1024² PNGs); make it a setting if people want more.
KEEP = 500
ID = r"^\d{13}-[0-9a-f]{32}$"  # created in ms, then the job id: names sort oldest first


class History:
    def __init__(self, folder: Path | None) -> None:
        self.folder = folder  # None: not kept (a host without a data folder)

    def record(self, job: Job) -> None:
        if self.folder is None or job.status != "done" or job.result is None or not job.result.images:
            return
        item_id = f"{int(job.created * 1000):013d}-{job.id}"
        image = job.result.images[0]  # shortcut: n > 1 keeps the first image only; the dashboard asks for one
        meta = {
            "id": item_id, "created": job.created, "prompt": job.prompt, "model": job.model, "style": job.style,
            "seed": job.result.seed, "elapsed_ms": job.elapsed_ms, "key": job.key, "file": item_id + image.extension,
            **job.params,
        }
        try:
            self.folder.mkdir(parents=True, exist_ok=True)
            (self.folder / meta["file"]).write_bytes(image.content)
            (self.folder / f"{item_id}.json").write_text(json.dumps(meta), encoding="utf-8")
            for old in sorted(self.folder.glob("*.json"))[:-KEEP]:
                for path in self.folder.glob(f"{old.stem}.*"):
                    path.unlink(missing_ok=True)
        except OSError:
            logger.exception("Couldn't keep job %s in the history", job.id)  # history must never fail a job

    def _read(self, path: Path) -> dict | None:
        try:
            meta = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return meta if isinstance(meta, dict) else None

    def list(self, q: str, limit: int, before: str | None, key: str | None) -> list[dict]:
        """Newest first. `key` None sees every image; otherwise only that key's."""
        if self.folder is None or not self.folder.is_dir():
            return []
        q, items = q.strip().lower(), []
        for path in sorted(self.folder.glob("*.json"), reverse=True):
            if before and path.stem >= before:
                continue
            meta = self._read(path)
            if meta is None or (key is not None and meta.get("key") != key):
                continue
            if q and q not in str(meta.get("prompt", "")).lower():
                continue
            items.append(_public(meta))
            if len(items) == limit:
                break
        return items

    def get(self, item_id: str, key: str | None) -> dict:
        meta = None
        if self.folder is not None and re.match(ID, item_id):
            meta = self._read(self.folder / f"{item_id}.json")
        if meta is None or (key is not None and meta.get("key") != key):
            raise ApiError("not_found", "That image isn't in the history any more.")
        try:
            data = (self.folder / Path(str(meta["file"])).name).read_bytes()
        except (OSError, KeyError):
            raise ApiError("not_found", "That image isn't in the history any more.") from None
        return {**_public(meta), "b64_json": base64.b64encode(data).decode("ascii")}


def _public(meta: dict) -> dict:
    return {k: v for k, v in meta.items() if k not in ("key", "file")}


class Profile(BaseModel):
    """A named set of Generate settings. Empty strings and None mean "the default"."""

    name: str = Field(min_length=1, max_length=60)
    model: str = Field(default="", max_length=200)
    style: str = Field(default="", max_length=200)
    shape: str = Field(default="", max_length=40)
    quality: Literal["", "fast", "standard", "best"] = ""
    steps: int | None = Field(default=None, ge=1, le=150)
    cfg: float | None = Field(default=None, ge=0.0, le=30.0)
    seed: int | None = Field(default=None, ge=0, le=2**63 - 1)  # None: a new variation each time
    negative: str = Field(default="", max_length=4000)


class Profiles(BaseModel):
    profiles: list[Profile] = Field(max_length=50)


def read_profiles(engine_file: Path | None) -> list[dict]:
    """The saved profiles; any that no longer validate are skipped rather than failing the page."""
    raw = get_setting(engine_file, "generate_profiles", [])
    out = []
    for item in raw if isinstance(raw, list) else []:
        try:
            out.append(Profile.model_validate(item).model_dump())
        except ValidationError:
            continue
    return out


router = APIRouter()


def _only(request: Request) -> str | None:
    """The main key sees every image; another key only its own."""
    key = request.state.key
    return None if key.owner else key.id


@router.get("/api/v1/history", dependencies=[Depends(require_api_key)])
async def history(
    request: Request,
    q: str = Query(default="", max_length=200),
    limit: int = Query(default=30, ge=1, le=100),
    before: str | None = Query(default=None, pattern=ID),
) -> dict:
    """Past images, newest first, without the image itself; page with `before` = the last id."""
    items = await asyncio.to_thread(request.app.state.history.list, q, limit, before, _only(request))
    return {"items": items, "next": items[-1]["id"] if len(items) == limit else None}


@router.get("/api/v1/history/{item_id}", dependencies=[Depends(require_api_key)])
async def history_item(item_id: str, request: Request) -> dict:
    return await asyncio.to_thread(request.app.state.history.get, item_id, _only(request))


@router.get("/api/v1/profiles", dependencies=[Depends(require_main_key)])
async def profiles(request: Request) -> dict:
    return {"profiles": read_profiles(request.app.state.settings.engine_file)}


@router.put("/api/v1/profiles", dependencies=[Depends(require_main_key)])
async def save_profiles(body: Profiles, request: Request) -> dict:
    path = request.app.state.settings.engine_file
    if path is None:
        raise ApiError("not_found", "Profiles are kept by the Glint Studio app; this host runs on its own.")
    names = [p.name.strip().lower() for p in body.profiles]
    if len(set(names)) != len(names):
        raise ApiError("bad_params", "Two profiles have the same name.")
    await asyncio.to_thread(update_settings, path, {"generate_profiles": [p.model_dump() for p in body.profiles]})
    return {"profiles": read_profiles(path)}
