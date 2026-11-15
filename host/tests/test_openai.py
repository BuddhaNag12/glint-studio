"""Slice 3: /v1/images/generations and the openai_compat provider."""

from __future__ import annotations

import base64
import json
import logging
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from app.errors import ApiError
from app.providers.base import ProviderRequest
from app.providers.openai_compat import load_remote_providers
from app.catalog import resolve_params
from app.styles import compose
from tests.test_api import TINY_PNG, _setup_test_app

AUTH = {"Authorization": "Bearer test-secret-key"}
EXAMPLE = Path(__file__).resolve().parents[1] / "providers.example.json"


def _providers_file(tmp_path: Path, **overrides) -> Path:
    data = json.loads(EXAMPLE.read_text())
    data["providers"][0].update(overrides)
    path = tmp_path / "providers.json"
    path.write_text(json.dumps(data))
    return path


def _mock(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _load(tmp_path: Path, handler, **overrides):
    providers, models = load_remote_providers(_providers_file(tmp_path, **overrides), set(), _mock(handler))
    return providers["openai"], models["gpt-image-1"]


def _job(model, n=1, seed=None):
    return ProviderRequest(model, compose(model, "a cat"), resolve_params(model, n=n), seed)


async def test_openai_compat_sends_only_supported_fields_and_reads_b64_and_url(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    seen: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        if req.url.path == "/img.png":
            return httpx.Response(200, content=TINY_PNG, headers={"content-type": "image/png"})
        return httpx.Response(200, json={"created": 1, "data": [
            {"b64_json": base64.b64encode(TINY_PNG).decode()},
            {"url": "https://cdn.example/img.png"},
        ]})

    provider, model = _load(tmp_path, handler)
    assert model.cloud and model.provider == "openai"
    result = await provider.generate(_job(model, n=2, seed=5))

    body = json.loads(seen[0].content)
    assert body == {"model": "gpt-image-1", "prompt": "a cat", "n": 2, "size": "1024x1024"}
    assert seen[0].headers["authorization"] == "Bearer sk-test"
    assert [img.content for img in result.images] == [TINY_PNG, TINY_PNG]
    assert result.images[0].mime_type == "image/png" and result.seed is None


@pytest.mark.parametrize(("status", "code"), [
    (401, "provider_auth"), (403, "provider_auth"), (429, "provider_quota"),
    (400, "provider_rejected"), (404, "model_missing"), (500, "provider_down"),
])
async def test_openai_compat_maps_http_errors(tmp_path, monkeypatch, status, code) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    provider, model = _load(tmp_path, lambda req: httpx.Response(
        status, json={"error": {"message": "nope from upstream"}}))
    with pytest.raises(ApiError) as err:
        await provider.generate(_job(model))
    assert err.value.code == code and "nope from upstream" in err.value.message


@pytest.mark.parametrize(("exc", "code"), [
    (httpx.ConnectError("refused"), "provider_down"),
    (httpx.ReadTimeout("slow"), "timeout"),
])
async def test_openai_compat_maps_network_errors(tmp_path, monkeypatch, exc, code) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")

    def handler(req):
        raise exc

    provider, model = _load(tmp_path, handler)
    with pytest.raises(ApiError) as err:
        await provider.generate(_job(model))
    assert err.value.code == code


async def test_openai_compat_without_a_key_is_provider_auth(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.chdir(tmp_path)  # no .env here
    provider, model = _load(tmp_path, lambda req: httpx.Response(200, json={"data": []}))
    with pytest.raises(ApiError) as err:
        await provider.generate(_job(model))
    assert err.value.code == "provider_auth" and "OPENAI_API_KEY" in err.value.message


def test_bad_providers_are_skipped_not_fatal(tmp_path, caplog) -> None:
    good = json.loads(EXAMPLE.read_text())["providers"][0]
    clash = {**good, "id": "other", "models": [{**good["models"][0], "id": "z-image-turbo"}]}
    broken = {"id": "broken", "type": "openai_compat"}
    path = tmp_path / "providers.json"
    path.write_text(json.dumps({"providers": [good, clash, broken, {**good}]}))
    with caplog.at_level(logging.WARNING):
        providers, models = load_remote_providers(path, {"z-image-turbo"}, _mock(lambda r: None))
    assert set(providers) == {"openai"} and set(models) == {"gpt-image-1"}
    assert caplog.text.count("Skipping provider") == 3


def test_images_endpoint_runs_the_local_default(tmp_path) -> None:
    app, fake, output_dir = _setup_test_app(tmp_path)
    with TestClient(app) as client:
        app.state.providers["comfyui"].client = fake
        res = client.post("/v1/images/generations", headers=AUTH,
                          json={"prompt": "a cat", "size": "832x1216", "style": "cartoon", "seed": 9,
                                "quality": "best", "user": "ignored"})
        assert res.status_code == 200, res.text
        body = res.json()
        assert body["data"] == [{"b64_json": base64.b64encode(TINY_PNG).decode()}]
        assert body["model"] == "z-image-turbo" and body["seed"] == 9
        nodes = fake.last_workflow
        assert nodes["67"]["inputs"]["text"].startswith("Cartoon illustration. a cat.")
        assert (nodes["68"]["inputs"]["width"], nodes["68"]["inputs"]["height"]) == (832, 1216)
        assert nodes["70"]["inputs"]["steps"] == 9

        url = client.post("/v1/images/generations", headers=AUTH,
                          json={"prompt": "a cat", "response_format": "url"}).json()
        assert url["data"][0]["url"].startswith("http://testserver/api/v1/images/cat-")
        assert len(list(output_dir.iterdir())) == 1


@pytest.mark.parametrize(("body", "needle"), [
    ({"prompt": "x", "model": "nope"}, "Unknown model"),
    ({"prompt": "x", "style": "nope"}, "Unknown style"),
    ({"prompt": "x", "size": "4096x4096"}, "pixels"),
    ({"prompt": "x", "size": "big"}, "size"),
    ({"prompt": "x", "n": 2}, "n must be"),
    ({"prompt": "   "}, "prompt"),
])
def test_images_endpoint_errors_use_the_error_format(tmp_path, body, needle) -> None:
    app, fake, _ = _setup_test_app(tmp_path)
    with TestClient(app) as client:
        app.state.providers["comfyui"].client = fake
        res = client.post("/v1/images/generations", headers=AUTH, json=body)
        assert res.status_code == 422
        assert res.json()["error"]["code"] == "bad_params"
        assert needle in res.json()["error"]["message"]
        assert fake.last_workflow is None
        # The legacy route keeps FastAPI's own validation body.
        assert "detail" in client.post("/api/v1/generate", headers=AUTH, json={"prompt": " "}).json()


def test_images_endpoint_routes_remote_models_to_their_provider(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    app, _, _ = _setup_test_app(tmp_path, providers_file=_providers_file(tmp_path))
    with TestClient(app) as client:
        app.state.providers["openai"].http = _mock(lambda req: httpx.Response(
            429, json={"error": {"message": "You exceeded your quota"}}))
        models = {m["id"]: m for m in client.get("/api/v1/models", headers=AUTH).json()["models"]}
        assert models["gpt-image-1"]["cloud"] is True and models["z-image-turbo"]["cloud"] is False
        assert "sk-test" not in json.dumps(models)

        res = client.post("/v1/images/generations", headers=AUTH,
                          json={"prompt": "x", "model": "gpt-image-1", "size": "1024x1536"})
        assert res.status_code == 429
        assert res.json()["error"]["code"] == "provider_quota"
