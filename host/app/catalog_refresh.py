"""Catalog refresh: new models without a new app release.

When catalog/ changes on main, CI publishes it as one file, catalog.json, on the "catalog" release. The host
asks for that file once when it starts (Settings can turn this off), keeps the models this app doesn't ship
in <app data>/online-catalog, and adds them to the model list. Each entry is checked exactly like a built-in one; an
entry this app can't read (written for a newer app) is skipped and counted as "needs an app update".

shortcut: remote adds new ids only, built-ins come from the app; a fixed built-in still needs an app release.

Build the file:  python -m app.catalog_refresh build ../catalog > catalog.json
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import shutil
import sys
import time
from pathlib import Path
from typing import Any

import httpx
from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, StrictBool, ValidationError

from app import net
from app.auth import require_main_key
from app.catalog import CatalogError, CatalogModel, load_catalog
from app.dashboard import _engine_file, local_only
from app.errors import ApiError
from app.model_folders import get_setting, update_settings

logger = logging.getLogger(__name__)

SCHEMA = 1
MAX_BYTES = 2_000_000  # the whole catalog is a few KB per model
SETTING = "catalog_refresh"
WORKFLOW_NAME = re.compile(r"^[A-Za-z0-9_-]+$")


def bundle(catalog_dir: Path) -> dict:
    """Every model and workflow in catalog/, as one JSON object."""
    load_catalog(catalog_dir)  # a broken catalog fails here, in CI, not on people's computers
    read = lambda p: json.loads(p.read_text(encoding="utf-8"))  # noqa: E731
    return {
        "schema": SCHEMA,
        "models": [read(p) for p in sorted((catalog_dir / "models").glob("*.json"))],
        "workflows": {p.name: read(p) for p in sorted((catalog_dir / "workflows").glob("*.json"))},
    }


def apply(data: Any, builtin: set[str], cache_dir: Path) -> list[str]:
    """Replace cache_dir with the bundle's models that aren't in `builtin`, and their workflows.
    Returns the ids of entries this app can't use."""
    if not isinstance(data, dict) or data.get("schema") != SCHEMA:
        raise CatalogError("The online catalog is for a newer version of the app.")
    workflows = data.get("workflows") if isinstance(data.get("workflows"), dict) else {}
    keep: dict[str, dict] = {}
    unusable: list[str] = []
    for raw in data.get("models") if isinstance(data.get("models"), list) else []:
        model_id = raw.get("id") if isinstance(raw, dict) else None
        try:
            model = CatalogModel.model_validate(raw)
        except ValidationError as exc:
            logger.info("Catalog entry %r skipped: %s", model_id, exc.errors()[0].get("msg"))
            unusable.append(str(model_id))
            continue
        if model.id in builtin:
            continue
        files = [f"{model.workflow}.json", f"{model.workflow}.mapping.json"] if model.workflow else []
        if model.workflow and not (WORKFLOW_NAME.match(model.workflow) and all(isinstance(workflows.get(f), dict) for f in files)):
            unusable.append(model.id)
            continue
        keep[model.id] = raw

    new = cache_dir.with_name(cache_dir.name + ".new")
    shutil.rmtree(new, ignore_errors=True)
    (new / "models").mkdir(parents=True)
    (new / "workflows").mkdir()
    for model_id, raw in keep.items():
        (new / "models" / f"{model_id}.json").write_text(json.dumps(raw, indent=2), encoding="utf-8")
        if workflow := raw.get("workflow"):
            for name in (f"{workflow}.json", f"{workflow}.mapping.json"):
                (new / "workflows" / name).write_text(json.dumps(workflows[name]), encoding="utf-8")
    shutil.rmtree(cache_dir, ignore_errors=True)
    new.rename(cache_dir)
    return unusable


def load_cached(cache_dir: Path | None, builtin: set[str]) -> dict[str, CatalogModel]:
    """The models saved by the last refresh; a broken cache is ignored, the next refresh replaces it."""
    if cache_dir is None or not (cache_dir / "models").is_dir():
        return {}
    try:
        models = load_catalog(cache_dir)
    except CatalogError as exc:
        logger.warning("Ignoring the saved online catalog: %s", exc)
        return {}
    return {k: v for k, v in models.items() if k not in builtin}


async def fetch(client: httpx.AsyncClient, url: str) -> Any:
    async with client.stream("GET", url, follow_redirects=True, timeout=30.0) as res:
        res.raise_for_status()
        body = bytearray()
        async for chunk in res.aiter_bytes():
            body += chunk
            if len(body) > MAX_BYTES:
                raise CatalogError("The online catalog is too large.")
    return json.loads(body)


class CatalogRefresh:
    """Keeps `models` (the app's shared model dict) in step with the online catalog."""

    def __init__(self, url: str, engine_file: Path | None, models: dict[str, CatalogModel], client: httpx.AsyncClient):
        self.url = url
        self.engine_file = engine_file
        self.cache_dir = engine_file.parent / "online-catalog" if engine_file else None
        self.models = models
        self.builtin = set(models)  # built-in and remote-provider models: never replaced
        self.client = client
        self.added: set[str] = set()
        self.unusable: list[str] = []
        self.checked_at: float | None = None
        self.error: str | None = None
        self._lock = asyncio.Lock()
        self._swap(load_cached(self.cache_dir, self.builtin))

    @property
    def available(self) -> bool:
        return bool(self.url and self.cache_dir)

    def enabled(self) -> bool:
        return get_setting(self.engine_file, SETTING, True) is not False

    def _swap(self, cached: dict[str, CatalogModel]) -> None:
        for model_id in self.added - cached.keys():
            self.models.pop(model_id, None)
        self.models.update(cached)
        self.added = set(cached)

    async def refresh(self) -> None:
        if not self.available:
            return
        async with self._lock:
            own = None
            try:
                own = net.client(net.read(self.engine_file))  # the proxy and CA file from Settings → Downloads
                data = await fetch(own or self.client, self.url)
                self.unusable = await asyncio.to_thread(apply, data, self.builtin, self.cache_dir)
                self._swap(await asyncio.to_thread(load_cached, self.cache_dir, self.builtin))
                self.error = None
            except httpx.HTTPStatusError as exc:  # the raw httpx text is a URL and an MDN link
                code = exc.response.status_code
                self.error = "No new models are published yet." if code == 404 else f"The server answered {code}."
                logger.warning("Catalog refresh failed: %s", exc)
            except httpx.TransportError as exc:
                self.error = "No connection to the catalog server."
                logger.warning("Catalog refresh failed: %s", exc)
            except (httpx.HTTPError, ValueError, OSError, CatalogError, ApiError) as exc:
                self.error = getattr(exc, "message", None) or str(exc) or type(exc).__name__
                logger.warning("Catalog refresh failed: %s", self.error)
            finally:
                self.checked_at = time.time()
                if own:
                    await own.aclose()

    def view(self) -> dict:
        return {
            "available": self.available,
            "enabled": self.enabled(),
            "checked_at": self.checked_at,
            "added": sorted(self.added),
            "needs_update": self.unusable,
            "error": self.error,
        }


router = APIRouter(prefix="/api/v1/catalog", dependencies=[Depends(require_main_key), Depends(local_only)])


class Toggle(BaseModel):
    enabled: StrictBool


@router.get("")
async def status(request: Request) -> dict:
    return request.app.state.catalog.view()


@router.put("")
async def set_enabled(body: Toggle, request: Request) -> dict:
    await asyncio.to_thread(update_settings, _engine_file(request), {SETTING: body.enabled})
    return request.app.state.catalog.view()


@router.post("/refresh")
async def refresh_now(request: Request) -> dict:
    """"Check now": runs even with the startup check turned off, since the user asked."""
    await request.app.state.catalog.refresh()
    return request.app.state.catalog.view()


if __name__ == "__main__":
    if sys.argv[1:2] != ["build"] or len(sys.argv) != 3:
        sys.exit("usage: python -m app.catalog_refresh build <catalog dir>")
    json.dump(bundle(Path(sys.argv[2])), sys.stdout, separators=(",", ":"))
