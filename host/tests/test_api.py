"""Integration tests for FastAPI endpoints, auth, CORS, image signing, and ComfyClient."""

from __future__ import annotations

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

    async def check_health(self, timeout_seconds: float = 5.0) -> None:
        if self.health_error:
            raise self.health_error

    async def generate_images(
        self,
        workflow: dict[str, Any],
        output_node_id: str | None = None,
        timeout_seconds: float = 120.0,
    ) -> tuple[str, list[ComfyImageOutput]]:
        if self.generate_error:
            raise self.generate_error
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
    (workflows_dir / "default.json").write_text(json.dumps(SAMPLE_WORKFLOW), encoding="utf-8")
    (workflows_dir / "default.mapping.json").write_text(json.dumps(SAMPLE_MAPPING), encoding="utf-8")

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
        app.state.comfy_client = fake_comfy
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


def test_generate_requires_valid_api_key_and_rejects_unconfigured_server(tmp_path: Path) -> None:
    app, fake_comfy, _ = _setup_test_app(tmp_path)
    with TestClient(app) as client:
        app.state.comfy_client = fake_comfy

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
        app.state.comfy_client = fake_comfy

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


def test_generate_both_serves_signed_image_url_and_base64(tmp_path: Path) -> None:
    app, fake_comfy, output_dir = _setup_test_app(tmp_path)
    with TestClient(app) as client:
        app.state.comfy_client = fake_comfy

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
        assert fake_comfy.last_workflow["6"]["inputs"]["text"] == "a golden teapot"
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
        app.state.comfy_client = fake_comfy

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
        app.state.comfy_client = fake_comfy
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
        app.state.comfy_client = fake_comfy
        res = client.post(
            "/api/v1/generate",
            headers={"Authorization": "Bearer test-secret-key"},
            json={"prompt": "sunset", "workflow": "nonexistent"},
        )
        assert res.status_code == 404


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
        prompt_id, images = await client.generate_images(SAMPLE_WORKFLOW, output_node_id="9")

    assert prompt_id == "ws-789"
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
        app.state.comfy_client = fake_comfy
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
