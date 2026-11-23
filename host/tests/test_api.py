"""Integration tests for FastAPI endpoints, auth, CORS, image signing, and ComfyClient."""

from __future__ import annotations

import asyncio
import base64
import json
import os
import struct
import time
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from fastapi.testclient import TestClient

from app.comfy_client import (
    ComfyClient,
    ComfyConnectionError,
    ComfyExecutionError,
    ComfyImageOutput,
    ComfyTimeoutError,
    _parse_comfy_dirs_from_argv,
)
from app.config import Settings
from app.main import create_app

# Valid 1x1 transparent PNG bytes.
TINY_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYAAAAAYAAjCB0C8AAAAASUVORK5CYII="
)

SAMPLE_WORKFLOW = {
    "3": {
        "class_type": "KSampler",
        "inputs": {"seed": 1, "steps": 20, "cfg": 7.0, "positive": ["6", 0], "negative": ["7", 0]},
    },
    "5": {"class_type": "EmptyLatentImage", "inputs": {"width": 512, "height": 512}},
    "6": {"class_type": "CLIPTextEncode", "inputs": {"text": "default"}},
    "7": {"class_type": "CLIPTextEncode", "inputs": {"text": ""}},
    "9": {"class_type": "SaveImage", "inputs": {"images": ["8", 0]}},
}

SAMPLE_MAPPING = {
    "positive_prompt": {"node_id": "6", "input_key": "text"},
    "negative_prompt": {"node_id": "7", "input_key": "text"},
    "seed": {"node_id": "3", "input_key": "seed"},
    "steps": {"node_id": "3", "input_key": "steps"},
    "cfg": {"node_id": "3", "input_key": "cfg"},
    "width": {"node_id": "5", "input_key": "width"},
    "height": {"node_id": "5", "input_key": "height"},
    "output_node_id": "9",
}


class FakeComfyClient:
    """Stub ComfyClient used to test FastAPI behavior without a running ComfyUI process."""

    def __init__(self) -> None:
        self.health_error: Exception | None = None
        self.generate_error: Exception | None = None
        self.last_workflow: dict[str, Any] | None = None
        self.last_output_node_id: str | None = None
        self.gate: asyncio.Event | None = None  # when set, runs block until it is released
        self.started: list[str] = []  # positive prompts, in the order runs began
        self.interrupted = 0

    async def check_health(self, timeout_seconds: float = 5.0) -> None:
        if self.health_error:
            raise self.health_error

    async def generate_images(
        self,
        workflow: dict[str, Any],
        output_node_id: str | None = None,
        timeout_seconds: float = 120.0,
        on_progress: Any = None,
        on_preview: Any = None,
    ) -> tuple[str, list[ComfyImageOutput]]:
        if self.generate_error:
            raise self.generate_error
        self.started.append(next((n["inputs"]["text"] for n in workflow.values()
                                  if n.get("class_type") == "CLIPTextEncode"), ""))
        if on_progress:
            on_progress(0.5)
        if self.gate is not None:
            try:
                await self.gate.wait()
            except asyncio.CancelledError:
                self.interrupted += 1
                raise
        self.last_workflow = workflow
        self.last_output_node_id = output_node_id
        return (
            "prompt-uuid-123",
            [
                ComfyImageOutput(
                    filename="ComfyUI_00001_.png",
                    content=TINY_PNG,
                    mime_type="image/png",
                    extension=".png",
                )
            ],
        )


def _setup_test_app(tmp_path: Path, **setting_overrides: Any):
    workflows_dir = tmp_path / "workflows"
    output_dir = tmp_path / "outputs"
    workflows_dir.mkdir(parents=True, exist_ok=True)
    # A user's own workflow; "default" runs the catalog model instead.
    (workflows_dir / "sample.json").write_text(json.dumps(SAMPLE_WORKFLOW), encoding="utf-8")
    (workflows_dir / "sample.mapping.json").write_text(json.dumps(SAMPLE_MAPPING), encoding="utf-8")

    config_kwargs: dict[str, Any] = {
        "api_key": "test-secret-key",
        "url_signing_secret": "test-signing-secret",
        "cors_origins": ["http://localhost:3000"],
        "public_base_url": "http://testserver",
        "workflows_dir": workflows_dir,
        "output_dir": output_dir,
    }
    config_kwargs.update(setting_overrides)
    cfg = Settings(**config_kwargs)
    app = create_app(cfg)
    fake_comfy = FakeComfyClient()
    return app, fake_comfy, output_dir


def test_health_endpoint_reports_ok_and_degraded(tmp_path: Path) -> None:
    app, fake_comfy, _ = _setup_test_app(tmp_path)
    with TestClient(app) as client:
        app.state.providers["comfyui"].client = fake_comfy
        res_ok = client.get("/health")
        assert res_ok.status_code == 200
        assert res_ok.json()["status"] == "ok"
        assert res_ok.json()["comfyui_connected"] is True
        assert res_ok.json()["default_workflow_present"] is True

        fake_comfy.health_error = ComfyConnectionError("Connection refused")
        res_down = client.get("/health")
        assert res_down.status_code == 503
        assert res_down.json()["status"] == "degraded"
        assert res_down.json()["comfyui_connected"] is False
        assert client.get("/livez").status_code == 200  # alive even with ComfyUI down

        app.state.settings.engine_installed = False  # the desktop app's setup hasn't installed it
        res_missing = client.get("/health")
        assert res_missing.status_code == 503
        assert res_missing.json()["detail"].startswith("Engine not installed")


def test_generate_requires_valid_api_key_and_rejects_unconfigured_server(tmp_path: Path) -> None:
    app, fake_comfy, _ = _setup_test_app(tmp_path)
    with TestClient(app) as client:
        app.state.providers["comfyui"].client = fake_comfy

        missing = client.post("/api/v1/generate", json={"prompt": "test"})
        assert missing.status_code == 401

        wrong = client.post(
            "/api/v1/generate",
            headers={"Authorization": "Bearer wrong-key"},
            json={"prompt": "test"},
        )
        assert wrong.status_code == 403

        ok_x_api_key = client.post(
            "/api/v1/generate",
            headers={"X-API-Key": "test-secret-key"},
            json={"prompt": "test"},
        )
        assert ok_x_api_key.status_code == 200

    unconfigured_app, _, _ = _setup_test_app(tmp_path, api_key="")
    with TestClient(unconfigured_app) as unconfigured_client:
        res = unconfigured_client.post(
            "/api/v1/generate",
            headers={"Authorization": "Bearer anything"},
            json={"prompt": "test"},
        )
        assert res.status_code == 500


def test_cors_preflight_allows_configured_origin_only(tmp_path: Path) -> None:
    app, _, _ = _setup_test_app(tmp_path)
    with TestClient(app) as client:
        allowed = client.options(
            "/api/v1/generate",
            headers={
                "Origin": "http://localhost:3000",
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "Authorization,Content-Type",
            },
        )
        assert allowed.status_code == 200
        assert allowed.headers.get("access-control-allow-origin") == "http://localhost:3000"

        # The app's Settings window is another origin and saves settings with PUT.
        put = client.options(
            "/api/v1/settings/security",
            headers={"Origin": "http://localhost:3000", "Access-Control-Request-Method": "PUT"},
        )
        assert put.status_code == 200

        disallowed = client.options(
            "/api/v1/generate",
            headers={
                "Origin": "https://untrusted.example",
                "Access-Control-Request-Method": "POST",
            },
        )
        assert "access-control-allow-origin" not in disallowed.headers


def test_generate_default_returns_raw_image_binary(tmp_path: Path) -> None:
    app, fake_comfy, output_dir = _setup_test_app(tmp_path)
    with TestClient(app) as client:
        app.state.providers["comfyui"].client = fake_comfy

        res = client.post(
            "/api/v1/generate",
            headers={
                "Authorization": "Bearer test-secret-key",
                "Origin": "http://localhost:3000",
            },
            json={
                "prompt": "Create a vertical mobile wallpaper of a golden teapot, 8k resolution, photorealistic",
                "seed": 777,
            },
        )
        assert res.status_code == 200
        assert res.headers["content-type"] == "image/png"
        assert res.headers["content-disposition"] == 'inline; filename="golden-teapot.png"'
        assert res.headers["x-image-title"] == "Golden Teapot"
        assert "X-Image-Title" in res.headers["access-control-expose-headers"]
        assert "Content-Disposition" in res.headers["access-control-expose-headers"]
        assert res.headers["x-prompt-id"] == "prompt-uuid-123"
        assert res.headers["x-seed"] == "777"
        assert "x-elapsed-ms" in res.headers
        assert res.content == TINY_PNG
        assert list(output_dir.iterdir()) == []


def test_image_links_use_the_address_the_caller_used(tmp_path: Path) -> None:
    # A phone on the LAN or through the tunnel can't open 127.0.0.1; it gets the address it called.
    app, fake_comfy, _ = _setup_test_app(tmp_path)
    with TestClient(app) as client:
        app.state.providers["comfyui"].client = fake_comfy
        res = client.post(
            "/api/v1/generate",
            headers={"Authorization": "Bearer test-secret-key", "Host": "192.168.1.20:7788"},
            json={"prompt": "a golden teapot", "response_format": "url"},
        )
        assert res.json()["image_url"].startswith("http://192.168.1.20:7788/api/v1/images/")


def test_generate_both_serves_signed_image_url_and_base64(tmp_path: Path) -> None:
    app, fake_comfy, output_dir = _setup_test_app(tmp_path)
    with TestClient(app) as client:
        app.state.providers["comfyui"].client = fake_comfy

        res = client.post(
            "/api/v1/generate",
            headers={"Authorization": "Bearer test-secret-key"},
            json={"prompt": "a golden teapot", "seed": 777, "response_format": "both"},
        )
        assert res.status_code == 200
        assert res.headers["x-image-title"] == "Golden Teapot"
        body = res.json()
        assert body["prompt_id"] == "prompt-uuid-123"
        assert body["seed"] == 777
        assert body["image_base64"] == base64.b64encode(TINY_PNG).decode("ascii")
        assert body["data_url"].startswith("data:image/png;base64,")
        assert body["image_url"].startswith("http://testserver/api/v1/images/golden-teapot-")

        assert fake_comfy.last_workflow is not None
        # "default" runs Z-Image Turbo through the Lock-screen Wallpaper style.
        nodes = fake_comfy.last_workflow
        assert nodes["67"]["inputs"]["text"].startswith("Ultra high-detail vertical mobile wallpaper")
        assert ". a golden teapot. Grounded tactile foreground" in nodes["67"]["inputs"]["text"]
        assert (nodes["68"]["inputs"]["width"], nodes["68"]["inputs"]["height"]) == (624, 1344)
        assert nodes["70"]["inputs"]["seed"] == 777
        assert fake_comfy.last_output_node_id == "9"

        parsed = urlsplit(body["image_url"])
        query = parse_qs(parsed.query)
        assert "exp" in query and "sig" in query

        img_res = client.get(f"{parsed.path}?{parsed.query}")
        assert img_res.status_code == 200
        assert img_res.headers["content-type"] == "image/png"
        assert img_res.content == TINY_PNG

        tampered = client.get(f"{parsed.path}?exp={query['exp'][0]}&sig=badsignature")
        assert tampered.status_code == 403

        expired = client.get(f"{parsed.path}?exp=1&sig={query['sig'][0]}")
        assert expired.status_code == 403

        missing_sig = client.get(parsed.path)
        assert missing_sig.status_code == 403

        assert len(list(output_dir.iterdir())) == 1


def test_extract_image_title_and_filename_slug() -> None:
    from app.naming import extract_image_title, safe_header_value, title_to_filename_slug

    long_prompt = (
        "Ultra high-detail vertical mobile wallpaper, 9:19.5 aspect ratio, iOS lock screen depth effect composition. "
        "An ancient double-decker living root bridge over an emerald pool, photorealistic, 8k resolution"
    )
    title = extract_image_title(long_prompt)
    assert title == "Ancient Double-Decker Living Root Bridge Over an Emerald" or title.startswith(
        "Ancient Double-Decker Living Root Bridge"
    )
    assert title_to_filename_slug(title).startswith("ancient-double-decker-living-root-bridge")
    assert extract_image_title("Title: Emerald Mist\nDeep jungle waterfall") == "Emerald Mist"
    assert extract_image_title("   neon   rain   alley  --ar 9:16 --v 6  ") == "Neon Rain Alley"
    assert title_to_filename_slug("   neon   rain   alley  ") == "neon-rain-alley"
    assert extract_image_title("   ") == "AI Wallpaper"
    assert title_to_filename_slug(extract_image_title("   ")) == "ai-wallpaper"
    assert safe_header_value("Café Rays\r\nBad") == "Cafe Rays Bad"


def test_generate_url_and_base64_exclusive_formats(tmp_path: Path) -> None:
    app, fake_comfy, output_dir = _setup_test_app(tmp_path)
    with TestClient(app) as client:
        app.state.providers["comfyui"].client = fake_comfy

        res_b64 = client.post(
            "/api/v1/generate",
            headers={"Authorization": "Bearer test-secret-key"},
            json={"prompt": "sunset", "response_format": "base64"},
        )
        assert res_b64.status_code == 200
        assert res_b64.json()["image_url"] is None
        assert res_b64.json()["image_base64"] is not None
        assert list(output_dir.iterdir()) == []

        res_url = client.post(
            "/api/v1/generate",
            headers={"Authorization": "Bearer test-secret-key"},
            json={"prompt": "sunset", "response_format": "url"},
        )
        assert res_url.status_code == 200
        assert res_url.json()["image_url"] is not None
        assert res_url.json()["image_base64"] is None
        assert res_url.json()["data_url"] is None


@pytest.mark.parametrize(
    ("error", "expected_status"),
    [
        (ComfyExecutionError("Missing checkpoint"), 502),
        (ComfyExecutionError("Prompt outputs failed validation: value_not_in_list"), 502),
        (ComfyConnectionError("Cannot connect"), 503),
        (ComfyTimeoutError("Timed out"), 504),
    ],
)
def test_generate_maps_comfy_errors_to_http_statuses(
    tmp_path: Path,
    error: Exception,
    expected_status: int,
) -> None:
    app, fake_comfy, _ = _setup_test_app(tmp_path)
    with TestClient(app) as client:
        app.state.providers["comfyui"].client = fake_comfy
        fake_comfy.generate_error = error
        res = client.post(
            "/api/v1/generate",
            headers={"Authorization": "Bearer test-secret-key"},
            json={"prompt": "sunset"},
        )
        assert res.status_code == expected_status


def test_generate_missing_workflow_returns_404(tmp_path: Path) -> None:
    app, fake_comfy, _ = _setup_test_app(tmp_path)
    with TestClient(app) as client:
        app.state.providers["comfyui"].client = fake_comfy
        res = client.post(
            "/api/v1/generate",
            headers={"Authorization": "Bearer test-secret-key"},
            json={"prompt": "sunset", "workflow": "nonexistent"},
        )
        assert res.status_code == 404


def test_generate_runs_a_users_own_workflow_as_is(tmp_path: Path) -> None:
    app, fake_comfy, _ = _setup_test_app(tmp_path)
    with TestClient(app) as client:
        app.state.providers["comfyui"].client = fake_comfy
        res = client.post(
            "/api/v1/generate",
            headers={"Authorization": "Bearer test-secret-key"},
            json={"prompt": "sunset", "workflow": "sample", "width": 4000, "height": 4000,
                  "negative_prompt": "blur", "response_format": "url"},
        )
        assert res.status_code == 200
        assert res.json()["workflow"] == "sample"
        assert fake_comfy.last_workflow["6"]["inputs"]["text"] == "sunset"
        assert fake_comfy.last_workflow["7"]["inputs"]["text"] == "blur"
        assert fake_comfy.last_workflow["5"]["inputs"]["width"] == 4000  # no catalog limits


@pytest.mark.parametrize("extra", [{"width": 4096, "height": 4096}, {"steps": 150}, {"width": 1024}])
def test_generate_default_rejects_params_outside_the_model_limits(tmp_path: Path, extra) -> None:
    app, fake_comfy, _ = _setup_test_app(tmp_path)
    with TestClient(app) as client:
        app.state.providers["comfyui"].client = fake_comfy
        res = client.post(
            "/api/v1/generate",
            headers={"Authorization": "Bearer test-secret-key"},
            json={"prompt": "sunset", **extra},
        )
        assert res.status_code == 422
        assert "detail" in res.json()  # legacy body shape
        assert fake_comfy.last_workflow is None


def test_images_take_steps_and_cfg_within_the_model_limits(tmp_path: Path) -> None:
    app, fake_comfy, _ = _setup_test_app(tmp_path)
    with TestClient(app, headers={"Authorization": "Bearer test-secret-key"}) as client:
        app.state.providers["comfyui"].client = fake_comfy
        res = client.post("/v1/images/generations", json={"prompt": "a fox", "steps": 9, "cfg": 1.5})
        assert res.status_code == 200
        inputs = [n["inputs"] for n in fake_comfy.last_workflow.values()]
        assert any(i.get("steps") == 9 for i in inputs) and any(i.get("cfg") == 1.5 for i in inputs)
        for extra in ({"steps": 21}, {"cfg": 2.5}):  # Z-Image Turbo allows steps 1-20, cfg 1-2
            res = client.post("/api/v1/jobs", json={"prompt": "a fox", **extra})
            assert res.status_code == 422 and res.json()["error"]["code"] == "bad_params"


def test_models_and_styles_endpoints(tmp_path: Path) -> None:
    app, _, _ = _setup_test_app(tmp_path)
    with TestClient(app) as client:
        auth = {"Authorization": "Bearer test-secret-key"}
        models = client.get("/api/v1/models", headers=auth).json()["models"]
        by_id = {m["id"]: m for m in models}
        assert set(by_id) == {"z-image-turbo", "dreamshaper-8-lcm", "flux-schnell"}
        assert by_id["z-image-turbo"]["limits"]["sizes"]["phone"] == [624, 1344]
        styles = {s["id"]: s for s in client.get("/api/v1/styles", headers=auth).json()["styles"]}
        assert styles["lockscreen-wallpaper"]["overrides"]["shape"] == "phone"

        missing = client.get("/api/v1/models")
        assert missing.status_code == 401
        assert missing.json() == {"error": {"code": "unauthorized", "message": missing.json()["error"]["message"]}}
        wrong = client.get("/api/v1/styles", headers={"X-API-Key": "nope"})
        assert wrong.status_code == 403 and wrong.json()["error"]["code"] == "forbidden"


def test_user_styles_dir_replaces_the_default_style(tmp_path: Path) -> None:
    user_styles = tmp_path / "styles"
    user_styles.mkdir()
    (user_styles / "lockscreen-wallpaper.json").write_text(
        json.dumps({"id": "lockscreen-wallpaper", "name": "Mine", "prefix": "MY PREFIX"})
    )
    app, fake_comfy, _ = _setup_test_app(tmp_path, user_styles_dir=user_styles)
    with TestClient(app) as client:
        app.state.providers["comfyui"].client = fake_comfy
        res = client.post(
            "/api/v1/generate",
            headers={"Authorization": "Bearer test-secret-key"},
            json={"prompt": "sunset", "response_format": "url"},
        )
        assert res.status_code == 200
        assert fake_comfy.last_workflow["67"]["inputs"]["text"] == "MY PREFIX. sunset"
        assert fake_comfy.last_workflow["68"]["inputs"]["width"] == 1024  # no shape override now


def test_styles_can_be_edited_reset_and_added(tmp_path: Path) -> None:
    user_styles = tmp_path / "user-styles"  # created on the first save
    app, _, _ = _setup_test_app(tmp_path, user_styles_dir=user_styles)
    auth = {"Authorization": "Bearer test-secret-key"}
    with TestClient(app) as client:

        def styles() -> dict:
            return {s["id"]: s for s in client.get("/api/v1/styles", headers=auth).json()["styles"]}

        original = styles()["cartoon"]
        assert original["builtin"] and not original["edited"]

        edit = {"id": "cartoon", "name": "My cartoon", "prefix": "ink"}
        assert client.put("/api/v1/styles/cartoon", headers=auth, json=edit).status_code == 200
        assert styles()["cartoon"]["name"] == "My cartoon" and styles()["cartoon"]["edited"]
        assert json.loads((user_styles / "cartoon.json").read_text()) == edit

        assert client.delete("/api/v1/styles/cartoon", headers=auth).status_code == 204
        assert styles()["cartoon"] == original  # reset to the shipped one

        mine = {"id": "my-style", "name": "Mine"}
        assert client.put("/api/v1/styles/my-style", headers=auth, json=mine).status_code == 200
        assert not styles()["my-style"]["builtin"]
        assert client.delete("/api/v1/styles/my-style", headers=auth).status_code == 204
        assert "my-style" not in styles()

        mismatch = client.put("/api/v1/styles/other", headers=auth, json=mine)
        assert mismatch.status_code == 422 and mismatch.json()["error"]["code"] == "bad_params"
        bad_id = client.put("/api/v1/styles/..%2Fx", headers=auth, json={"id": "..%2Fx", "name": "x"})
        assert bad_id.status_code in (404, 422)
        assert client.delete("/api/v1/styles/cartoon", headers=auth).status_code == 404
        assert client.put("/api/v1/styles/my-style", json=mine).status_code == 401


@pytest.mark.parametrize(
    ("error", "code"),
    [
        (ComfyExecutionError("ComfyUI rejected workflow: {'type': 'value_not_in_list'}"), "model_missing"),
        (ComfyExecutionError("CUDA out of memory"), "workflow_failed"),
        (ComfyConnectionError("refused"), "engine_down"),
        (ComfyTimeoutError("slow"), "timeout"),
    ],
)
async def test_comfyui_provider_maps_failures_to_error_codes(error: Exception, code: str) -> None:
    from app.catalog import load_catalog, resolve_params
    from app.errors import ApiError
    from app.providers.base import ProviderRequest
    from app.providers.comfyui import ComfyUIProvider
    from app.styles import compose

    catalog = Path(__file__).resolve().parents[2] / "catalog"
    model = load_catalog(catalog)["z-image-turbo"]
    fake = FakeComfyClient()
    fake.generate_error = error
    provider = ComfyUIProvider(fake, catalog / "workflows", 5.0)  # type: ignore[arg-type]
    with pytest.raises(ApiError) as err:
        await provider.generate(ProviderRequest(model, compose(model, "x"), resolve_params(model)))
    assert err.value.code == code


async def test_comfyui_provider_loads_the_installed_variants_files(tmp_path: Path) -> None:
    from app.catalog import load_catalog, resolve_params
    from app.providers.base import ProviderRequest
    from app.providers.comfyui import ComfyUIProvider
    from app.styles import compose

    catalog = Path(__file__).resolve().parents[2] / "catalog"
    model = load_catalog(catalog)["z-image-turbo"]
    int8 = next(v for v in model.variants if v.precision == "int8")
    for f in int8.files:  # sparse files of the right size count as downloaded
        (tmp_path / f.dest).parent.mkdir(parents=True, exist_ok=True)
        with (tmp_path / f.dest).open("wb") as out:
            out.truncate(f.bytes)
    fake = FakeComfyClient()
    provider = ComfyUIProvider(fake, catalog / "workflows", 5.0, roots=lambda: [tmp_path])  # type: ignore[arg-type]
    await provider.generate(ProviderRequest(model, compose(model, "x"), resolve_params(model)))
    names = {n["class_type"]: n["inputs"] for n in (fake.last_workflow or {}).values()}
    assert names["UNETLoader"]["unet_name"] == "z_image_turbo_int8_convrot.safetensors"
    assert names["CLIPLoader"]["clip_name"] == "qwen_3_4b_fp8_mixed.safetensors"
    assert names["VAELoader"]["vae_name"] == "ae.safetensors"


@pytest.mark.parametrize(("vram", "unet"), [(16.0, "z_image_turbo_bf16.safetensors"), (8.0, "z_image_turbo_int8_convrot.safetensors")])
async def test_comfyui_provider_runs_the_downloaded_variant_that_fits(tmp_path: Path, vram: float, unet: str) -> None:
    from app.catalog import load_catalog, resolve_params
    from app.providers.base import ProviderRequest
    from app.providers.comfyui import ComfyUIProvider
    from app.styles import compose

    catalog = Path(__file__).resolve().parents[2] / "catalog"
    model = load_catalog(catalog)["z-image-turbo"]
    for f in {f.dest: f for v in model.variants for f in v.files}.values():  # both variants downloaded
        (tmp_path / f.dest).parent.mkdir(parents=True, exist_ok=True)
        with (tmp_path / f.dest).open("wb") as out:
            out.truncate(f.bytes)
    fake = FakeComfyClient()
    hw = {"backend": "cuda", "vram_budget_gb": vram}
    provider = ComfyUIProvider(fake, catalog / "workflows", 5.0, roots=lambda: [tmp_path], hardware=lambda: hw)  # type: ignore[arg-type]
    await provider.generate(ProviderRequest(model, compose(model, "x"), resolve_params(model)))
    names = {n["class_type"]: n["inputs"] for n in (fake.last_workflow or {}).values()}
    assert names["UNETLoader"]["unet_name"] == unet


class _FakeWebSocket:
    def __init__(self, messages: list[Any]) -> None:
        self._messages = iter(messages)

    async def __aenter__(self) -> _FakeWebSocket:
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        return None

    async def recv(self) -> Any:
        return next(self._messages)


@pytest.mark.asyncio
async def test_comfy_client_ws_flow_skips_binary_frames_and_downloads_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/prompt":
            return httpx.Response(200, json={"prompt_id": "abc-123"})
        if request.url.path == "/history/abc-123":
            return httpx.Response(
                200,
                json={
                    "abc-123": {
                        "outputs": {
                            "2": {
                                "images": [
                                    {"filename": "preview.png", "subfolder": "", "type": "temp"}
                                ]
                            },
                            "9": {
                                "images": [
                                    {"filename": "final.png", "subfolder": "", "type": "output"}
                                ]
                            },
                        }
                    }
                },
            )
        if request.url.path == "/view":
            assert request.url.params["filename"] == "final.png"
            return httpx.Response(200, content=TINY_PNG)
        return httpx.Response(404)

    ws_messages = [
        b"\x00\x01binary-preview-frame",
        json.dumps({"type": "executing", "data": {"prompt_id": "other-job", "node": None}}),
        json.dumps({"type": "executing", "data": {"prompt_id": "abc-123", "node": "3"}}),
        json.dumps({"type": "executing", "data": {"prompt_id": "abc-123", "node": None}}),
    ]
    monkeypatch.setattr(
        "app.comfy_client.websockets.connect",
        lambda url, max_size=None: _FakeWebSocket(ws_messages),
    )

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as http_client:
        client = ComfyClient("http://127.0.0.1:8188", "ws://127.0.0.1:8188", http_client)
        prompt_id, images = await client.generate_images(SAMPLE_WORKFLOW, output_node_id="9")

    assert prompt_id == "abc-123"
    assert len(images) == 1
    assert images[0].filename == "final.png"
    assert images[0].content == TINY_PNG
    assert images[0].mime_type == "image/png"


@pytest.mark.asyncio
async def test_comfy_client_raises_on_ws_execution_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/prompt":
            return httpx.Response(200, json={"prompt_id": "err-456"})
        return httpx.Response(404)

    ws_messages = [
        json.dumps(
            {
                "type": "execution_error",
                "data": {
                    "prompt_id": "err-456",
                    "node_id": "4",
                    "node_type": "CheckpointLoaderSimple",
                    "exception_message": "Checkpoint not found",
                },
            }
        )
    ]
    monkeypatch.setattr(
        "app.comfy_client.websockets.connect",
        lambda url, max_size=None: _FakeWebSocket(ws_messages),
    )

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as http_client:
        client = ComfyClient("http://127.0.0.1:8188", "ws://127.0.0.1:8188", http_client)
        with pytest.raises(ComfyExecutionError, match="Checkpoint not found"):
            await client.generate_images(SAMPLE_WORKFLOW)


@pytest.mark.asyncio
async def test_comfy_client_cleans_up_temp_file_and_history_with_cache_fallback(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    comfy_root = tmp_path / "ComfyUI"
    temp_dir = comfy_root / "temp"
    temp_dir.mkdir(parents=True)
    temp_file = temp_dir / "ComfyUI_temp_abcde_00001_.png"
    temp_file.write_bytes(TINY_PNG)
    deleted_history_ids: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/prompt":
            return httpx.Response(200, json={"prompt_id": "clean-123"})
        if request.url.path == "/system_stats":
            return httpx.Response(
                200,
                json={
                    "system": {
                        "argv": [
                            str(tmp_path / "ElectronApp" / "main.py"),
                            "--base-directory",
                            str(comfy_root),
                        ]
                    }
                },
            )
        if request.url.path == "/history/clean-123":
            return httpx.Response(
                200,
                json={
                    "clean-123": {
                        "outputs": {
                            "9": {
                                "images": [
                                    {
                                        "filename": "ComfyUI_temp_abcde_00001_.png",
                                        "subfolder": "",
                                        "type": "temp",
                                    }
                                ]
                            }
                        }
                    }
                },
            )
        if request.url.path == "/view":
            if not temp_file.is_file():
                return httpx.Response(404)
            return httpx.Response(200, content=temp_file.read_bytes())
        if request.url.path == "/history" and request.method == "POST":
            payload = json.loads(request.content.decode("utf-8"))
            deleted_history_ids.extend(payload.get("delete", []))
            return httpx.Response(200)
        return httpx.Response(404)

    def make_ws_messages() -> list[Any]:
        return [
            json.dumps({"type": "executing", "data": {"prompt_id": "clean-123", "node": "9"}}),
            json.dumps({"type": "executing", "data": {"prompt_id": "clean-123", "node": None}}),
        ]

    monkeypatch.setattr(
        "app.comfy_client.websockets.connect",
        lambda url, max_size=None: _FakeWebSocket(make_ws_messages()),
    )

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as http_client:
        client = ComfyClient("http://127.0.0.1:8188", "ws://127.0.0.1:8188", http_client)
        _, images = await client.generate_images(SAMPLE_WORKFLOW, output_node_id="9")
        assert images[0].content == TINY_PNG
        assert not temp_file.exists()
        assert deleted_history_ids == ["clean-123"]

        # Re-running an identical cached job still succeeds even though temp_file was deleted.
        _, cached_images = await client.generate_images(SAMPLE_WORKFLOW, output_node_id="9")
        assert cached_images[0].content == TINY_PNG


@pytest.mark.asyncio
async def test_comfy_client_supports_save_image_websocket_without_disk_or_view(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    deleted_history_ids: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/prompt":
            return httpx.Response(200, json={"prompt_id": "ws-789"})
        if request.url.path == "/history" and request.method == "POST":
            payload = json.loads(request.content.decode("utf-8"))
            deleted_history_ids.extend(payload.get("delete", []))
            return httpx.Response(200)
        raise AssertionError(f"Unexpected HTTP call: {request.method} {request.url.path}")

    header = struct.pack(">II", 1, 2)
    ws_messages = [
        json.dumps({"type": "executing", "data": {"prompt_id": "ws-789", "node": "70"}}),
        b"\x00\x00\x00\x01\x00\x00\x00\x01sampling-preview",
        json.dumps({"type": "executing", "data": {"prompt_id": "ws-789", "node": "9"}}),
        header + TINY_PNG,
        json.dumps({"type": "executing", "data": {"prompt_id": "ws-789", "node": None}}),
    ]
    monkeypatch.setattr(
        "app.comfy_client.websockets.connect",
        lambda url, max_size=None: _FakeWebSocket(ws_messages),
    )

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as http_client:
        client = ComfyClient("http://127.0.0.1:8188", "ws://127.0.0.1:8188", http_client)
        previews: list[bytes] = []
        prompt_id, images = await client.generate_images(SAMPLE_WORKFLOW, output_node_id="9", on_preview=previews.append)

    assert prompt_id == "ws-789"
    assert previews == [b"sampling-preview"]  # the sampler's frame is a preview, never an output image
    assert len(images) == 1
    assert images[0].content == TINY_PNG
    assert images[0].mime_type == "image/png"
    assert deleted_history_ids == ["ws-789"]


def test_generate_url_mode_prunes_expired_output_files(tmp_path: Path) -> None:
    app, fake_comfy, output_dir = _setup_test_app(tmp_path, image_url_ttl_seconds=60)
    output_dir.mkdir(parents=True, exist_ok=True)
    stale_file = output_dir / "00000000000000000000000000000000-1.png"
    stale_file.write_bytes(TINY_PNG)
    old_ts = time.time() - 3600
    os.utime(stale_file, (old_ts, old_ts))

    with TestClient(app) as client:
        app.state.providers["comfyui"].client = fake_comfy
        assert not stale_file.exists()

        res = client.post(
            "/api/v1/generate",
            headers={"Authorization": "Bearer test-secret-key"},
            json={"prompt": "test", "response_format": "url"},
        )
        assert res.status_code == 200
        assert len(list(output_dir.iterdir())) == 1


def test_settings_and_argv_expand_tilde_home_paths() -> None:
    cfg = Settings(comfyui_dir="~/Documents/ComfyUI", comfyui_temp_dir="  ")
    assert cfg.comfyui_dir == Path("~/Documents/ComfyUI").expanduser()
    assert cfg.comfyui_temp_dir is None

    base_dir, temp_dir, output_dir = _parse_comfy_dirs_from_argv(
        [
            "/Applications/ComfyUI.app/Contents/Resources/ComfyUI/main.py",
            "--base-directory",
            "~/Documents/ComfyUI",
        ]
    )
    assert base_dir == Path("~/Documents/ComfyUI").expanduser()
    assert temp_dir is None
    assert output_dir is None


def test_cors_origins_from_env_accepts_comma_list_and_json(monkeypatch) -> None:
    monkeypatch.setenv("CORS_ORIGINS", "tauri://localhost, http://tauri.localhost")
    assert Settings().cors_origins == ["tauri://localhost", "http://tauri.localhost"]
    monkeypatch.setenv("CORS_ORIGINS", '["http://a"]')
    assert Settings().cors_origins == ["http://a"]
