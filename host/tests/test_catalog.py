"""Slice 1: error codes, the catalog, styles and param checks."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.catalog import CatalogError, ResolvedParams, check_params, load_catalog, resolve_params
from app.errors import ERROR_STATUS, ApiError, api_error_handler
from app.schemas import GenerateRequest
from app.styles import Style, compose, load_styles
from app.workflow import load_workflow_bundle, prepare_workflow

REPO = Path(__file__).resolve().parents[2]
CATALOG = REPO / "catalog"


@pytest.fixture(scope="module")
def models():
    return load_catalog(CATALOG)


@pytest.fixture(scope="module")
def styles():
    return load_styles(CATALOG / "styles")


@pytest.fixture
def zit(models):
    return models["z-image-turbo"]


def test_shipped_catalog_and_styles_load(models, styles) -> None:
    assert "z-image-turbo" in models
    assert {"ai-art", "cartoon", "realistic", "anime", "3d-render", "pixel-art",
            "watercolor", "cinematic", "lockscreen-wallpaper"} <= set(styles)


def test_lockscreen_style_reproduces_the_old_workflow_prompt(zit, styles) -> None:
    # The old workflow joined its two StringConcatenate nodes around the prompt with ". ".
    lock = styles["lockscreen-wallpaper"]
    user = "A golden teapot on a mossy rock"
    assert compose(zit, user, style=lock).positive == f"{lock.prefix}. {user}. {lock.suffix}"


def test_compose_strips_doubled_periods_and_handles_no_style(zit, styles) -> None:
    assert compose(zit, "  a cat.  ").positive == "a cat."
    assert compose(zit, "a cat.", style=styles["cartoon"]).positive.startswith(
        "Cartoon illustration. a cat. bold clean outlines"
    )


def test_negatives_are_additive_and_dropped_for_models_without_them(zit, styles) -> None:
    model = zit.model_copy(update={
        "negative": "model-neg",
        "limits": zit.limits.model_copy(update={"supports_negative": True}),
    })
    out = compose(model, "x", style=styles["cartoon"], negative="user-neg")
    assert out.negative == f"{styles['cartoon'].negative}, model-neg, user-neg"
    assert not out.negative_dropped

    # Z-Image Turbo runs at cfg 1, where ComfyUI skips the negative entirely.
    out = compose(zit, "x", style=styles["cartoon"], negative="user-neg")
    assert out.negative is None and out.negative_dropped
    assert not compose(zit, "x").negative_dropped


def test_style_limited_to_other_models_is_rejected(zit) -> None:
    style = Style(id="only-flux", name="Only Flux", models=["flux-dev"])
    with pytest.raises(ApiError) as err:
        compose(zit, "x", style=style)
    assert err.value.code == "bad_params"


def test_resolve_precedence_request_then_style_then_quality_then_default(zit, styles) -> None:
    assert resolve_params(zit) == ResolvedParams(1024, 1024, 6, 1.0, 1)
    assert resolve_params(zit, quality="best").steps == 9
    lock = styles["lockscreen-wallpaper"]
    assert (resolve_params(zit, style=lock).width, resolve_params(zit, style=lock).height) == (624, 1344)
    assert resolve_params(zit, style=lock, shape="square").width == 1024
    assert resolve_params(zit, quality="best", steps=3).steps == 3
    assert resolve_params(zit, width=768, height=1024).height == 1024


@pytest.mark.parametrize("kwargs", [
    {"width": 4096, "height": 4096},
    {"width": 1000, "height": 1000},  # not a multiple of 16
    {"width": 64, "height": 1024},
    {"width": 1024},
    {"steps": 150},
    {"cfg": 7.5},
    {"n": 2},
    {"shape": "banner"},
])
def test_out_of_limit_params_are_bad_params(zit, kwargs) -> None:
    with pytest.raises(ApiError) as err:
        resolve_params(zit, **kwargs)
    assert err.value.code == "bad_params"
    assert err.value.status_code == 422


def test_fixed_size_models_accept_only_their_sizes(zit) -> None:
    fixed = zit.model_copy(update={"limits": zit.limits.model_copy(update={"custom_size": False})})
    check_params(fixed, ResolvedParams(624, 1344, None, None, 1))
    with pytest.raises(ApiError):
        check_params(fixed, ResolvedParams(768, 1024, None, None, 1))


def test_catalog_rejects_presets_outside_limits(tmp_path: Path) -> None:
    (tmp_path / "models").mkdir()
    (tmp_path / "workflows").mkdir()
    (tmp_path / "workflows" / "z-image-turbo.json").write_text("{}")
    data = json.loads((CATALOG / "models" / "z-image-turbo.json").read_text())
    data["quality"]["best"]["steps"] = 99
    (tmp_path / "models" / "z-image-turbo.json").write_text(json.dumps(data))
    with pytest.raises(CatalogError, match="best"):
        load_catalog(tmp_path)


def test_catalog_rejects_model_files_outside_the_models_folder(tmp_path: Path) -> None:
    (tmp_path / "models").mkdir()
    (tmp_path / "workflows").mkdir()
    (tmp_path / "workflows" / "z-image-turbo.json").write_text("{}")
    data = json.loads((CATALOG / "models" / "z-image-turbo.json").read_text())
    data["variants"][0]["files"][0]["dest"] = "vae/../../escape.safetensors"
    (tmp_path / "models" / "z-image-turbo.json").write_text(json.dumps(data))
    with pytest.raises(CatalogError, match="dest"):
        load_catalog(tmp_path)


def test_every_variant_has_the_files_its_workflow_loads(models) -> None:
    # shortcut: workflows name their model files, so each variant must ship those exact names;
    # a smaller variant (fp8, GGUF) needs the host to inject file names into the workflow first.
    for model in models.values():
        if not model.variants:
            continue
        workflow = json.loads((CATALOG / "workflows" / f"{model.workflow}.json").read_text())
        loaded = {
            v for node in workflow.values() for v in node.get("inputs", {}).values()
            if isinstance(v, str) and v.endswith((".safetensors", ".gguf", ".ckpt"))
        }
        assert loaded, model.id
        for variant in model.variants:
            assert loaded <= {f.dest.split("/")[-1] for f in variant.files}, (model.id, variant.precision)


def test_user_styles_replace_builtins_and_bad_ones_are_skipped(tmp_path: Path) -> None:
    (tmp_path / "cartoon.json").write_text(json.dumps({"id": "cartoon", "name": "My Cartoon"}))
    (tmp_path / "broken.json").write_text("{not json")
    styles = load_styles(CATALOG / "styles", tmp_path)
    assert styles["cartoon"].name == "My Cartoon"
    assert "broken" not in styles


def test_catalog_workflow_takes_the_composed_prompt(zit, styles) -> None:
    workflow, mapping = load_workflow_bundle(CATALOG / "workflows", zit.workflow)
    positive = compose(zit, "a cat", style=styles["cartoon"]).positive
    payload, _ = prepare_workflow(workflow, mapping, GenerateRequest(prompt=positive))
    assert payload["67"]["inputs"]["text"] == positive
    assert workflow["67"]["inputs"]["text"] == ""  # template untouched


def test_api_error_renders_code_and_message() -> None:
    app = FastAPI()
    app.add_exception_handler(ApiError, api_error_handler)

    @app.get("/boom")
    async def boom():
        raise ApiError("provider_quota", "Out of credits.")

    res = TestClient(app).get("/boom")
    assert res.status_code == 429
    assert res.json() == {"error": {"code": "provider_quota", "message": "Out of credits."}}
    assert len(ERROR_STATUS) == 13
    with pytest.raises(ValueError):
        ApiError("nope", "x")  # type: ignore[arg-type]
