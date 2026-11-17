"""Models page: the memory bar, Discover tiers, and per-model settings."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.catalog import ModelSettings, check_settings, load_catalog, resolve_params, with_user_settings
from app.errors import ApiError
from app.local_models import memory_split, recommend, unlockable
from tests.test_local_models import hw, model, variant

CATALOG = Path(__file__).resolve().parents[2] / "catalog"
AUTH = {"Authorization": "Bearer test-secret-key"}


def test_memory_split_matches_the_recommender_budget() -> None:
    mac = memory_split({**hw("mps", 9.6, 16, 500), "gpus": [{"name": "Apple M3", "vram_gb": 16}]})
    assert mac["unified"] and mac["total_gb"] == 16 and mac["models_gb"] == 9.6
    assert mac["headroom_gb"] == pytest.approx(2.4) and mac["system_gb"] == pytest.approx(4)
    pc = memory_split({**hw("cuda", 24, 64, 500), "gpus": [{"name": "RTX 4090", "vram_gb": 24}]})
    assert (pc["total_gb"], pc["models_gb"], pc["headroom_gb"], pc["system_gb"]) == (24, 24, 0, 0)
    cpu = memory_split(hw("cpu", 0, 16, 500))
    assert cpu["models_gb"] == 12 and cpu["headroom_gb"] == 0


def test_tiers_best_faster_better_other() -> None:
    models = [
        model("huge", 95, [variant("bf16", 30, 30)]),  # runs only partly on a 16 GB GPU: better, slower
        model("mid", 80, [variant("bf16", 12, 12)]),  # the best pick
        model("tiny", 50, [variant("bf16", 4, 3)]),  # 4x fewer weights per step: faster
        model("same", 60, [variant("bf16", 10, 10)]),  # about as fast: best too
        model("worse", 40, [variant("bf16", 40, 40)]),  # slow and lower quality: other
        model("never", 99, [variant("bf16", 200, 30)]),  # won't fit at all: other
    ]
    rows = {r["id"]: r for r in recommend(hw("cuda", 16, 64, 500), models)}
    assert {k: r["tier"] for k, r in rows.items()} == {
        "mid": "best", "same": "best", "tiny": "faster", "huge": "better", "worse": "other", "never": "other",
    }
    assert rows["tiny"]["speed_note"] == "4× faster than mid (estimate)"


def test_measured_speed_wins_over_the_estimate() -> None:
    models = [model("mid", 80, [variant("bf16", 12, 12)]), model("tiny", 50, [variant("bf16", 4, 3)])]
    rows = recommend(hw("cuda", 16, 64, 500), models, measured={"mid": 6.0, "tiny": 2.0}.get)
    assert rows[1]["speed_note"] == "3× faster than mid"
    rows = recommend(hw("cuda", 16, 64, 500), models, measured={"mid": 6.0, "tiny": 5.0}.get)
    assert rows[1]["tier"] == "best"  # measured: not much faster after all


def test_unlock_lists_models_a_bigger_memory_guard_would_fit() -> None:
    mac = {**hw("mps", 9.6, 16, 500), "gpus": [{"name": "Apple M3", "vram_gb": 16}]}
    models = [model("fits", 80, [variant("bf16", 8, 8)]), model("bigger", 90, [variant("bf16", 11, 11)]), model("huge", 95, [variant("bf16", 30, 30)])]
    assert unlockable(mac, recommend(mac, models)) == ["bigger"]
    pc = {**hw("cuda", 8, 64, 500), "gpus": [{"name": "RTX", "vram_gb": 8}]}
    assert unlockable(pc, recommend(pc, models)) == []  # the budget already is the whole GPU


@pytest.fixture()
def zit():
    return load_catalog(CATALOG)["z-image-turbo"]


def test_settings_are_checked_against_the_model_limits(zit) -> None:
    check_settings(zit, ModelSettings(width=768, height=1024, steps=9, cfg=1.5))
    for bad in (
        ModelSettings(width=768),
        ModelSettings(width=770, height=1024),
        ModelSettings(steps=21),
        ModelSettings(cfg=3),
        ModelSettings(negative="blurry"),  # Z-Image Turbo takes no negative prompt
    ):
        with pytest.raises(ApiError):
            check_settings(zit, bad)


def test_user_settings_are_defaults_the_request_still_wins(tmp_path: Path, zit) -> None:
    engine = tmp_path / "engine.json"
    engine.write_text(json.dumps({"model_overrides": {"z-image-turbo": {"width": 768, "height": 1024, "steps": 9}}}))
    mine = with_user_settings(engine, zit)
    assert resolve_params(mine) == resolve_params(zit, width=768, height=1024, steps=9)
    assert resolve_params(mine, steps=4, shape="square").steps == 4
    assert resolve_params(mine, shape="square").width == 1024
    engine.write_text(json.dumps({"model_overrides": {"z-image-turbo": {"steps": 99}}}))
    assert with_user_settings(engine, zit) is zit  # an invalid saved entry falls back to the catalog


def test_settings_endpoint_and_jobs_use_it(tmp_path: Path) -> None:
    from tests.test_api import _setup_test_app

    app, fake, _ = _setup_test_app(tmp_path, engine_file=tmp_path / "engine.json")
    with TestClient(app, headers=AUTH) as client:
        app.state.providers["comfyui"].client = fake
        url = "/api/v1/models/z-image-turbo/settings"
        assert client.get(url).json()["settings"] == {}
        assert client.put(url, json={"steps": 99}).status_code == 422
        assert client.put("/api/v1/models/nope/settings", json={}).status_code == 404
        assert client.put(url, json={"steps": 9}).json()["settings"] == {"steps": 9}

        assert client.post("/v1/images/generations", json={"prompt": "a fox"}).status_code == 200
        assert any(n["inputs"].get("steps") == 9 for n in fake.last_workflow.values())
        assert client.post("/v1/images/generations", json={"prompt": "a fox", "steps": 3}).status_code == 200
        assert any(n["inputs"].get("steps") == 3 for n in fake.last_workflow.values())

        assert client.put(url, json={}).json()["settings"] == {}  # reset
        assert "model_overrides" in json.loads((tmp_path / "engine.json").read_text())


def test_aggressive_guard_raises_the_budget_on_shared_memory_only(tmp_path) -> None:
    import json

    from app.local_models import RAM_SHARE, load_hardware
    from app.model_folders import update_settings

    engine_file, hw_file = tmp_path / "engine.json", tmp_path / "hardware.json"
    hw_file.write_text(json.dumps({"backend": "mps", "vram_budget_gb": 9.6, "ram_gb": 16.0}))
    assert load_hardware(hw_file, tmp_path, engine_file)["vram_budget_gb"] == 9.6
    update_settings(engine_file, {"memory_guard": "aggressive"})
    assert load_hardware(hw_file, tmp_path, engine_file)["vram_budget_gb"] == 16.0 * RAM_SHARE
    hw_file.write_text(json.dumps({"backend": "cuda", "vram_budget_gb": 12.0, "ram_gb": 32.0}))
    assert load_hardware(hw_file, tmp_path, engine_file)["vram_budget_gb"] == 12.0
