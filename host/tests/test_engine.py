"""Linked model folders and keeping a model in memory."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.catalog import load_catalog
from app.config import Settings
from app.engine import Engine
from app.errors import ApiError
from app.local_models import model_installed
from app.model_folders import (
    candidates,
    extra_model_paths,
    file_present,
    get_setting,
    normalize,
    read_settings,
    save_settings,
    update_settings,
)

CATALOG = Settings().catalog_dir
MODELS = load_catalog(CATALOG)
# The real files are ~20 GB; Windows writes every byte of a truncated file, so the tests use tiny ones.
ZIT = MODELS["z-image-turbo"] = MODELS["z-image-turbo"].model_copy(deep=True)
for _f in ZIT.variants[0].files:
    _f.bytes = 16


def _install(root: Path, model, rename: dict[str, str] | None = None) -> None:
    """Files of the catalog's size, at dest or at a renamed category folder."""
    for f in model.variants[0].files:
        category, name = f.dest.split("/")
        p = root / (rename or {}).get(category, category) / name
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("wb") as fh:
            fh.truncate(f.bytes)


def test_yaml_quotes_windows_paths_and_lists_old_folder_names() -> None:
    text = extra_model_paths([r"C:\Users\o'neil\ComfyUI\models"])
    assert r"base_path: 'C:\Users\o''neil\ComfyUI\models'" in text
    assert "  diffusion_models: |\n    diffusion_models\n    unet\n" in text
    assert "  text_encoders: |\n    text_encoders\n    clip\n" in text


def test_settings_round_trip_and_yaml_goes_with_the_last_folder(tmp_path: Path) -> None:
    engine_file = tmp_path / "engine.json"
    assert read_settings(engine_file) == {"preload": False, "folders": []}  # off until turned on
    assert read_settings(None)["preload"] is False  # the host on its own
    save_settings(engine_file, False, ["/a"])
    assert read_settings(engine_file) == {"preload": False, "folders": ["/a"]}
    assert (tmp_path / "extra_model_paths.yaml").is_file()
    update_settings(engine_file, {"other": 1})
    save_settings(engine_file, False, [])
    assert not (tmp_path / "extra_model_paths.yaml").exists()
    assert get_setting(engine_file, "other") == 1  # features' own keys survive a save
    engine_file.write_text("{broken")
    with pytest.raises(ValueError):
        update_settings(engine_file, {"other": 2})
    assert engine_file.read_text() == "{broken"  # the user's file is never replaced by the patch
    assert not list(tmp_path.glob("*.tmp"))


def test_normalize_takes_the_comfyui_folder_or_its_models_folder(tmp_path: Path) -> None:
    (tmp_path / "ComfyUI" / "models").mkdir(parents=True)
    models = str((tmp_path / "ComfyUI" / "models").resolve())
    assert str(normalize(str(tmp_path / "ComfyUI"))) == models
    assert str(normalize(f'"{models}"')) == models
    assert normalize(str(tmp_path / "missing")) is None


def test_candidates_read_comfyui_desktop_config(tmp_path: Path) -> None:
    base = tmp_path / "Elsewhere" / "ComfyUI"
    (base / "models").mkdir(parents=True)
    (tmp_path / "Documents" / "ComfyUI" / "models").mkdir(parents=True)
    appdata = tmp_path / "AppData"
    (appdata / "ComfyUI").mkdir(parents=True)
    (appdata / "ComfyUI" / "config.json").write_text(json.dumps({"basePath": str(base)}))
    found = candidates(home=tmp_path, env={"APPDATA": str(appdata)}, system="Windows")
    assert str((base / "models").resolve()) in found
    assert str((tmp_path / "Documents" / "ComfyUI" / "models").resolve()) in found


def test_installed_counts_linked_folders_and_old_folder_names(tmp_path: Path) -> None:
    own, linked = tmp_path / "own", tmp_path / "linked"
    own.mkdir()
    assert not model_installed([own, linked], ZIT)
    _install(linked, ZIT, {"diffusion_models": "unet", "text_encoders": "clip"})
    assert model_installed([own, linked], ZIT)
    first = ZIT.variants[0].files[0]
    assert not file_present([own, linked], first.dest, first.bytes + 1)  # wrong size isn't it


class FakeProvider:
    def __init__(self) -> None:
        self.up = True
        self.runs: list[tuple[str, int, int]] = []
        self.freed = 0
        self.client = SimpleNamespace(free=self._free)

    async def _free(self) -> None:
        self.freed += 1

    async def system_stats(self) -> dict:
        if not self.up:
            raise ApiError("engine_down", "down")
        return {}

    async def generate(self, request, on_progress=None, on_preview=None):
        self.runs.append((request.model.id, request.params.width, request.params.steps))


def _engine(tmp_path: Path, provider: FakeProvider, preload: bool = True) -> Engine:
    return Engine(provider, MODELS, lambda: [tmp_path], lambda: preload, "z-image-turbo")  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_engine_preloads_on_start_and_again_after_a_restart(tmp_path: Path) -> None:
    provider = FakeProvider()
    engine = _engine(tmp_path, provider)
    _install(tmp_path, ZIT)
    watcher = asyncio.create_task(engine.watch(interval=0.01))
    await asyncio.sleep(0.05)
    assert engine.view() == {"status": "loaded", "model": "z-image-turbo", "error": None}
    assert provider.runs == [("z-image-turbo", 256, 1)]
    provider.up = False
    await asyncio.sleep(0.05)
    provider.up = True
    await asyncio.sleep(0.05)
    watcher.cancel()
    assert len(provider.runs) == 2

    await engine.unload()
    assert engine.view()["status"] == "idle" and provider.freed == 1
    engine.finished(SimpleNamespace(status="done", model="z-image-turbo"))  # type: ignore[arg-type]
    assert engine.view()["status"] == "loaded"


@pytest.mark.asyncio
async def test_engine_without_a_downloaded_model_stays_idle(tmp_path: Path) -> None:
    engine = _engine(tmp_path, FakeProvider())
    with pytest.raises(ApiError):
        engine.load()
    watcher = asyncio.create_task(engine.watch(interval=0.01))
    await asyncio.sleep(0.03)
    watcher.cancel()
    assert engine.view()["status"] == "idle"
