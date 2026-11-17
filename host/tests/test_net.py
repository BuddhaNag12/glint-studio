"""Download source and proxy settings, keyless generation on the LAN, and keys that can't open the dashboard."""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from app import net
from app.catalog import ModelFile, Variant
from app.dashboard import login_signature
from app.errors import ApiError
from app.local_models import Downloads
from tests.test_api import _setup_test_app

AUTH = {"Authorization": "Bearer test-secret-key"}
LOCAL = ("127.0.0.1", 50000)
HF_FILE = "https://huggingface.co/Comfy-Org/Z-Image-Turbo/resolve/main/split_files/vae/ae.safetensors"


def test_file_urls_follow_the_source() -> None:
    assert net.file_url(HF_FILE, net.DEFAULT) == HF_FILE
    mirror = net.DownloadSettings(hf_endpoint="https://hf-mirror.com/")
    assert net.file_url(HF_FILE, mirror) == "https://hf-mirror.com/Comfy-Org/Z-Image-Turbo/resolve/main/split_files/vae/ae.safetensors"
    ms = net.DownloadSettings(source="modelscope", hf_endpoint="https://hf-mirror.com")
    assert net.file_url(HF_FILE, ms) == "https://modelscope.cn/models/Comfy-Org/Z-Image-Turbo/resolve/master/split_files/vae/ae.safetensors"
    # A Hugging Face URL that isn't a file falls back to the mirror; other hosts never move.
    assert net.file_url("https://huggingface.co/Comfy-Org/Z-Image-Turbo", ms) == "https://hf-mirror.com/Comfy-Org/Z-Image-Turbo"
    assert net.file_url("https://example.test/m.safetensors", ms) == "https://example.test/m.safetensors"
    assert net.file_url("https://huggingface.co.evil.test/a/b/resolve/main/c", mirror) == "https://huggingface.co.evil.test/a/b/resolve/main/c"


def test_settings_are_validated(tmp_path: Path) -> None:
    for bad in [{"hf_endpoint": "hf-mirror.com"}, {"https_proxy": "socks5://p:1080"}, {"http_proxy": "http://p:1?x=1"},
                {"ca_bundle": str(tmp_path / "missing.pem")}, {"ca_bundle": "relative.pem"}, {"source": "github"}]:
        with pytest.raises(ValueError):
            net.DownloadSettings(**bad)
    ca = tmp_path / "ca.pem"
    ca.write_text("x")
    s = net.DownloadSettings(hf_endpoint=" ", https_proxy="http://user:pw@proxy:3128/", ca_bundle=str(ca))
    assert s.hf_endpoint is None and s.https_proxy == "http://user:pw@proxy:3128" and s.ca_bundle == str(ca)
    # Read back from engine.json: a CA file deleted since it was saved is a clear error, not a silent fallback.
    engine = tmp_path / "engine.json"
    assert net.read(engine) == net.DEFAULT
    engine.write_text(json.dumps({"downloads": {"ca_bundle": str(tmp_path / "gone.pem")}}))
    with pytest.raises(ApiError, match="ca_bundle"):
        net.read(engine)


def test_proxies_skip_no_proxy_hosts() -> None:
    assert net.client(net.DEFAULT) is None  # the host's usual client
    c = net.client(net.DownloadSettings(https_proxy="http://proxy:3128", no_proxy="localhost, .corp.test"))
    assert c._transport_for_url(httpx.URL("https://huggingface.co/x")) is not c._transport
    assert c._transport_for_url(httpx.URL("https://a.corp.test/x")) is c._transport
    assert c._transport_for_url(httpx.URL("http://localhost/x")) is c._transport
    everything = net.client(net.DownloadSettings(https_proxy="http://proxy:3128", no_proxy="*"))
    assert everything._transport_for_url(httpx.URL("https://huggingface.co/x")) is everything._transport


def test_a_wrong_mirror_fails_on_the_sha_and_says_where_to_look(tmp_path: Path) -> None:
    body = b"real model bytes"
    engine = tmp_path / "engine.json"
    engine.write_text(json.dumps({"downloads": {"source": "modelscope"}}))
    asked: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        asked.append(str(request.url))
        return httpx.Response(200, content=b"x" * len(body))  # right size, wrong bytes

    f = ModelFile(url=HF_FILE, sha256=hashlib.sha256(body).hexdigest(), bytes=len(body), dest="vae/ae.safetensors")

    async def run() -> dict:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            d = Downloads(client, tmp_path / "models", engine_file=engine)
            d.start("zit", Variant(precision="bf16", min_vram_gb=0, files=[f]))
            await d._task
            return d.view("zit")

    state = asyncio.run(run())
    assert asked == ["https://modelscope.cn/models/Comfy-Org/Z-Image-Turbo/resolve/master/split_files/vae/ae.safetensors"]
    assert state["status"] == "failed" and "corrupt" in state["error"] and "download settings" in state["error"]
    assert not (tmp_path / "models/vae/ae.safetensors").exists()


def test_download_settings_endpoints(tmp_path: Path) -> None:
    engine = tmp_path / "engine.json"
    engine.write_text(json.dumps({"preload": True}))
    app, _, _ = _setup_test_app(tmp_path, engine_file=engine, keys_file=tmp_path / "keys.json", allowed_hosts=["testserver"])
    with TestClient(app, client=LOCAL) as client:
        assert client.get("/api/v1/settings/downloads", headers=AUTH).json() == {**net.DEFAULT.model_dump(), "error": None}
        res = client.put("/api/v1/settings/downloads", json={"source": "huggingface", "ca_bundle": "/nope.pem"}, headers=AUTH)
        assert res.status_code == 422 and "ca_bundle" in res.json()["error"]["message"]
        res = client.put("/api/v1/settings/downloads", json={"hf_endpoint": "https://hf-mirror.com/"}, headers=AUTH)
        assert res.json()["hf_endpoint"] == "https://hf-mirror.com"
        saved = json.loads(engine.read_text())
        assert saved["preload"] is True and saved["downloads"]["hf_endpoint"] == "https://hf-mirror.com"
        # Only from this computer.
        lan = TestClient(app, client=("192.168.1.20", 50000))
        assert lan.get("/api/v1/settings/downloads", headers={**AUTH, "Host": "testserver"}).status_code == 400


# --- keyless generation from the LAN ---

def _lan_app(tmp_path: Path, on: bool = True):
    engine = tmp_path / "engine.json"
    engine.write_text(json.dumps({"lan_open_generation": on}))
    app, fake, _ = _setup_test_app(
        tmp_path, engine_file=engine, network_file=tmp_path / "network.json", keys_file=tmp_path / "keys.json",
        serve_on_network=True, allowed_hosts=["testserver"],
    )
    return app, fake, engine


def test_lan_open_generation_only_for_direct_lan_requests_to_generation_routes(tmp_path: Path) -> None:
    app, fake, engine = _lan_app(tmp_path)
    body = {"prompt": "a fox", "response_format": "b64_json"}
    phone = TestClient(app, client=("192.168.1.20", 50000), base_url="http://192.168.1.5:7788")
    with TestClient(app, client=LOCAL) as client:
        app.state.providers["comfyui"].client = fake
        # From the LAN, no key: making images and following them works.
        assert phone.post("/v1/images/generations", json=body).status_code == 200
        job = phone.post("/api/v1/jobs", json=body).json()["id"]
        assert phone.get(f"/api/v1/jobs/{job}").status_code == 200
        assert phone.get("/api/v1/models").status_code == 200
        assert TestClient(app, client=("fe80::1", 1), base_url="http://mac.local:7788").get("/api/v1/styles").status_code == 200
        # ...as a guest that can't see the main key's jobs, or use cloud models.
        main_job = client.post("/api/v1/jobs", json=body, headers=AUTH).json()["id"]
        assert phone.get(f"/api/v1/jobs/{main_job}").status_code == 404
        model = app.state.models["z-image-turbo"]
        app.state.models["z-image-turbo"] = model.model_copy(update={"cloud": True})
        assert phone.post("/v1/images/generations", json=body).json()["error"]["code"] == "forbidden"
        app.state.models["z-image-turbo"] = model

        # Never the dashboard's data, settings, keys or admin routes.
        for method, path in [("GET", "/api/v1/keys"), ("POST", "/api/v1/keys"), ("GET", "/api/v1/network"),
                             ("GET", "/api/v1/stats"), ("GET", "/api/v1/local-models"), ("GET", "/api/v1/settings/security"),
                             ("PUT", "/api/v1/settings/security"), ("DELETE", "/api/v1/styles/cartoon"),
                             ("POST", "/api/v1/engine/load"), ("POST", "/api/v1/generate")]:
            assert phone.request(method, path, json={}).status_code == 401, path
        assert phone.get("/api/v1/dashboard/connect").status_code == 400

        # Never through the tunnel or any proxy, from loopback or a public address, or under a rebound name.
        tunnel = TestClient(app, client=LOCAL, base_url="https://x.trycloudflare.com")
        assert tunnel.post("/v1/images/generations", json=body).status_code == 401
        assert tunnel.post("/v1/images/generations", json=body, headers={"X-Forwarded-For": "192.168.1.20"}).status_code == 401
        for headers in [{"X-Forwarded-For": "192.168.1.30"}, {"Cf-Connecting-Ip": "1.2.3.4"}, {"Forwarded": "for=1.2.3.4"}]:
            assert phone.post("/v1/images/generations", json=body, headers=headers).status_code == 401
        assert TestClient(app, client=("8.8.8.8", 1)).post("/v1/images/generations", json=body).status_code == 401
        assert TestClient(app, client=("100.64.0.9", 1), base_url="http://100.64.0.1").get("/api/v1/models").status_code == 401
        rebound = TestClient(app, client=("192.168.1.20", 50000), base_url="http://evil.test:7788")
        assert rebound.post("/v1/images/generations", json=body).status_code == 401
        # A wrong key is still wrong.
        assert phone.post("/v1/images/generations", json=body, headers={"X-API-Key": "nope"}).status_code == 403

        # Switched off, or LAN sharing off: a key again.
        engine.write_text(json.dumps({"lan_open_generation": "yes"}))  # anything but true is off
        assert phone.post("/v1/images/generations", json=body).status_code == 401
        engine.write_text(json.dumps({"lan_open_generation": True}))
        app.state.settings.serve_on_network = False
        assert phone.post("/v1/images/generations", json=body).status_code == 401


def test_security_settings_endpoints(tmp_path: Path) -> None:
    app, _, engine = _lan_app(tmp_path, on=False)
    with TestClient(app, client=LOCAL) as client:
        assert client.get("/api/v1/settings/security", headers=AUTH).json() == {"lan_open_generation": False, "lan_active": True}
        res = client.put("/api/v1/settings/security", json={"lan_open_generation": True}, headers=AUTH)
        assert res.json()["lan_open_generation"] is True and json.loads(engine.read_text())["lan_open_generation"] is True
        assert client.put("/api/v1/settings/security", json={"lan_open_generation": "on"}, headers=AUTH).status_code == 422
        token = client.post("/api/v1/keys", json={"name": "App"}, headers=AUTH).json()["key"]
        res = client.put("/api/v1/settings/security", json={"lan_open_generation": False}, headers={"X-API-Key": token})
        assert res.json()["error"]["code"] == "forbidden"
        lan = TestClient(app, client=("192.168.1.20", 50000))
        assert lan.put("/api/v1/settings/security", json={"lan_open_generation": False}, headers={**AUTH, "Host": "testserver"}).status_code == 400


def test_extra_keys_cannot_open_the_dashboard(tmp_path: Path) -> None:
    page = tmp_path / "dash"
    page.mkdir()
    (page / "dashboard.html").write_text("<h1>dash</h1>")
    (page / "phone.html").write_text("<h1>phone</h1>")
    app, _, _ = _setup_test_app(tmp_path, dashboard_dir=page, keys_file=tmp_path / "keys.json", allowed_hosts=["testserver"])
    with TestClient(app, client=LOCAL) as client:
        token = client.post("/api/v1/keys", json={"name": "Phone"}, headers=AUTH).json()["key"]
        extra = {"Authorization": f"Bearer {token}"}
        exp = int(time.time()) + 30
        # A login link signed with an extra key isn't one; nor does the key stand in for the session cookie.
        assert client.get(f"/login?exp={exp}&sig={login_signature(token, exp)}", follow_redirects=False).status_code == 403
        assert client.get("/", headers=extra).status_code == 403
        assert client.get("/api/v1/dashboard/connect", headers=extra).json()["error"]["code"] == "unauthorized"
        for path in ["/api/v1/settings/downloads", "/api/v1/network", "/api/v1/logs"]:
            assert client.get(path, headers=extra).json()["error"]["code"] == "forbidden", path
        # The phone page, which pairing opens with an extra key, still loads and the key still makes images.
        assert client.get("/m").text == "<h1>phone</h1>"
        assert client.get("/api/v1/styles", headers=extra).status_code == 200
