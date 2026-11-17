"""Per-model settings: the user's own size, steps, cfg and negative prompt for a model.

Kept in engine.json under "model_overrides" ({model_id: {width?, height?, steps?, cfg?, negative?}})
and applied by catalog.with_user_settings when a request doesn't set those values itself.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, Request

from app.auth import require_main_key
from app.catalog import CatalogModel, ModelSettings, check_settings, read_user_settings
from app.errors import ApiError
from app.model_folders import get_setting, update_settings

router = APIRouter()
KEY = "model_overrides"


def _model(request: Request, model_id: str) -> CatalogModel:
    model = request.app.state.models.get(model_id)
    if model is None:
        raise ApiError("not_found", f"No model '{model_id}'.")
    return model


def _engine_file(request: Request) -> Path:
    path = request.app.state.settings.engine_file
    if path is None:
        raise ApiError("not_found", "Model settings are kept by the Glint Studio app; this host runs on its own.")
    return path


def _view(engine_file: Path, model: CatalogModel) -> dict:
    saved = read_user_settings(engine_file, model)
    width, height = model.limits.sizes[model.defaults.shape]
    return {
        "settings": saved.model_dump(exclude_none=True) if saved else {},
        "defaults": {"width": width, "height": height, "steps": model.defaults.steps, "cfg": model.defaults.cfg, "negative": model.negative},
        "limits": model.limits.model_dump(mode="json"),
    }


@router.get("/api/v1/models/{model_id}/settings", dependencies=[Depends(require_main_key)])
async def get_model_settings(model_id: str, request: Request) -> dict:
    return _view(_engine_file(request), _model(request, model_id))


@router.put("/api/v1/models/{model_id}/settings", dependencies=[Depends(require_main_key)])
async def put_model_settings(model_id: str, body: ModelSettings, request: Request) -> dict:
    """Replaces this model's settings; an empty body resets it to the catalog's defaults."""
    model = _model(request, model_id)
    path = _engine_file(request)
    check_settings(model, body)
    saved = get_setting(path, KEY, {})
    saved = {k: v for k, v in saved.items() if k != model.id} if isinstance(saved, dict) else {}
    if values := body.model_dump(exclude_none=True):
        saved[model.id] = values
    update_settings(path, {KEY: saved})
    return _view(path, model)
