"""Model recommendations, resumable downloads, the dashboard login and its data endpoints."""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from pathlib import Path

import httpx
from fastapi.testclient import TestClient

from app.catalog import CatalogModel, ModelFile, Variant, load_catalog
from app.config import Settings
from app.dashboard import Sessions, login_signature
from app.errors import ApiError
from app.keys import Keys
from app.local_models import Downloads, chosen_variant, recommend
from tests.test_api import _setup_test_app

GB = 1e9
CATALOG = Path(__file__).resolve().parents[2] / "catalog"
AUTH = {"Authorization": "Bearer test-secret-key"}
LOCAL = ("127.0.0.1", 50000)  # TestClient's own address isn't this computer's


def hw(backend: str, vram: float, ram: float, disk: float) -> dict:
    return {"backend": backend, "vram_budget_gb": vram, "ram_gb": ram, "disk_free_gb": disk}


def variant(precision: str, min_vram: float, gb: float) -> Variant:
    f = ModelFile.model_construct(url="", sha256="", bytes=int(gb * GB), dest="")
    return Variant.model_construct(precision=precision, min_vram_gb=min_vram, files=[f])


def model(model_id: str, rank: int, variants: list[Variant]) -> CatalogModel:
    return CatalogModel.model_construct(id=model_id, name=model_id, license=None, quality_rank=rank, variants=variants)


def zit() -> CatalogModel:
    return model("zit", 80, [variant("bf16", 12, 20.7), variant("fp8", 7, 12)])


def pick(profile: dict, m: CatalogModel) -> tuple[str, str]:
    r = recommend(profile, [m])[0]
    return r["fit"], r["precision"]


def test_picks_the_best_variant_that_fits_the_gpu() -> None:
    assert pick(hw("cuda", 24, 64, 500), zit()) == ("great", "bf16")
    assert pick(hw("cuda", 8, 32, 500), zit()) == ("great", "fp8")
    # Apple Silicon 24 GB: the scan budgets 60%, 14.4 GB.
    assert pick(hw("mps", 14.4, 24, 500), zit()) == ("great", "bf16")
    assert pick(hw("mps", 9.6, 16, 500), zit()) == ("great", "fp8")


def test_too_little_gpu_memory_runs_slow_or_not_at_all() -> None:
    assert pick(hw("cuda", 4, 32, 500), zit()) == ("slow", "fp8")
    assert pick(hw("cpu", 0, 16, 500), zit()) == ("slow", "fp8")
    assert pick(hw("cpu", 0, 8, 500), zit())[0] == "wont_fit"


def test_needs_the_disk_space_unless_already_installed() -> None:
    low_disk = hw("cuda", 24, 64, 20)
    assert pick(low_disk, zit())[0] == "wont_fit"
    assert recommend(low_disk, [zit()], lambda _v: True)[0]["fit"] == "great"


def test_sorts_by_fit_then_quality_and_skips_cloud_models() -> None:
    models = [
        model("big", 95, [variant("bf16", 40, 30)]),
        model("small", 50, [variant("bf16", 4, 5)]),
        zit(),
        model("cloud", 99, []),
    ]
    assert [r["id"] for r in recommend(hw("cuda", 16, 32, 500), models)] == ["zit", "small", "big"]


def test_shipped_catalog_runs_great_on_a_24gb_mac() -> None:
    zimage = load_catalog(CATALOG)["z-image-turbo"]
    assert chosen_variant(hw("mps", 14.4, 24, 500), zimage).precision == "bf16"


# --- downloads ---

BODY = b"glint test model" * 1000
SHA = hashlib.sha256(BODY).hexdigest()


def _server(requests: list[dict]) -> httpx.AsyncClient:
    """Serves BODY and honours Range, like Hugging Face's CDN."""

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(dict(request.headers))
        rng = request.headers.get("range")
        if rng:
            start = int(rng.removeprefix("bytes=").rstrip("-"))
            return httpx.Response(206, content=BODY[start:])
        return httpx.Response(200, content=BODY)

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _variant(sha: str = SHA) -> Variant:
    f = ModelFile(url="https://example.test/m.safetensors", sha256=sha, bytes=len(BODY), dest="vae/t.safetensors")
    return Variant(precision="bf16", min_vram_gb=0, files=[f])


def test_download_resumes_a_part_file_and_checks_the_sha(tmp_path: Path) -> None:
    (tmp_path / "vae").mkdir()
    (tmp_path / "vae/t.safetensors.part").write_bytes(BODY[: len(BODY) // 2])
    requests: list[dict] = []

    async def run() -> dict:
        async with _server(requests) as client:
            d = Downloads(client, tmp_path)
            d.start("zit", _variant())
            await d._task
            return d.view("zit")

    state = asyncio.run(run())
    assert state["status"] == "done" and state["done"] == len(BODY)
    assert requests[0]["range"] == f"bytes={len(BODY) // 2}-"
    assert (tmp_path / "vae/t.safetensors").read_bytes() == BODY
    assert not (tmp_path / "vae/t.safetensors.part").exists()


def test_a_wrong_sha_throws_the_file_away(tmp_path: Path) -> None:
    async def run() -> dict:
        async with _server([]) as client:
            d = Downloads(client, tmp_path)
            d.start("zit", _variant("0" * 64))
            await d._task
            return d.view("zit")

    state = asyncio.run(run())
    assert state["status"] == "failed" and "corrupt" in state["error"]
    assert not list((tmp_path / "vae").iterdir())


def test_one_download_at_a_time(tmp_path: Path) -> None:
    async def run() -> None:
        async with _server([]) as client:
            d = Downloads(client, tmp_path)
            d.start("a", _variant())
            try:
                d.start("b", _variant())
                raise AssertionError("a second download started")
            except ApiError as exc:
                assert exc.code == "bad_params"
            await d.cancel()

    asyncio.run(run())


# --- dashboard login ---

def test_login_links_are_signed_short_lived_and_single_use() -> None:
    s, now = Sessions("key"), time.time()
    exp = int(now) + 30
    assert s.login(exp, "0" * 64, now) is None
    assert s.login(int(now) - 1, login_signature("key", int(now) - 1), now) is None  # expired
    assert s.login(int(now) + 3600, login_signature("key", int(now) + 3600), now) is None  # too long
    session = s.login(exp, login_signature("key", exp), now)
    assert session and s.valid(session)
    assert s.login(exp, login_signature("key", exp), now) is None  # spent
    assert not s.valid("made-up") and not s.valid(None) and not s.valid(".") and not s.valid("1.abc")
    assert Sessions("key").valid(session)  # a restarted host still knows it
    assert not Sessions("new-key").valid(session)  # a new key ends it
    assert Sessions("").login(exp, login_signature("", exp), now) is None


def test_login_signature_matches_the_desktop_app() -> None:
    # The same value is asserted in apps/desktop/src-tauri/src/dashboard.rs.
    assert login_signature("test-key", 1700000000) == "15a0213761010c51206ec491d60fffd0bc42a8254be811130cedec1122b7aa75"


def test_dashboard_login_cookie_and_connect(tmp_path: Path) -> None:
    page = tmp_path / "dash"
    (page / "assets").mkdir(parents=True)
    (page / "dashboard.html").write_text("<h1>dash</h1>")
    (page / "assets/app.js").write_text("1")
    app, _, _ = _setup_test_app(tmp_path, dashboard_dir=page, allowed_hosts=["testserver"])
    with TestClient(app, client=LOCAL) as client:
        assert client.get("/").status_code == 403
        assert client.get("/api/v1/dashboard/connect").json()["error"]["code"] == "unauthorized"
        exp = int(time.time()) + 30
        res = client.get(f"/login?exp={exp}&sig={login_signature('test-secret-key', exp)}", follow_redirects=False)
        assert res.status_code == 303
        cookie = res.headers["set-cookie"].lower()
        assert "httponly" in cookie and "samesite=strict" in cookie
        assert client.get("/").text == "<h1>dash</h1>"
        assert client.get("/assets/app.js").text == "1"
        assert client.get("/assets/..%2Fdashboard.html").status_code == 404
        assert client.get("/api/v1/dashboard/connect").json() == {
            "url": "http://testserver", "api_key": "test-secret-key"
        }
        # Another host name for the same address (DNS rebinding) is turned away from the cookie pages,
        # while the API, which needs the key, answers any name (a LAN address, the tunnel).
        evil = {"Host": "evil.test"}
        assert client.get("/", headers=evil).status_code == 400
        assert client.get("/api/v1/dashboard/connect", headers=evil).status_code == 400
        assert client.get("/api/v1/styles", headers={**evil, **AUTH}).status_code == 200


def test_network_settings_for_the_app(tmp_path: Path) -> None:
    net, tunnel = tmp_path / "network.json", tmp_path / "tunnel.json"
    app, _, _ = _setup_test_app(
        tmp_path, network_file=net, tunnel_file=tunnel, allowed_hosts=["testserver"], public_base_url="http://h:7788"
    )
    with TestClient(app, client=LOCAL) as client:
        view = client.get("/api/v1/network", headers=AUTH).json()
        assert view["lan"] == {"on": False, "active": False, "url": None}
        assert view["tunnel"] == {"on": False, "status": "off", "url": None, "error": None}
        assert client.get("/api/v1/network").status_code == 401

        res = client.put("/api/v1/network", json={"tunnel": True}, headers=AUTH)
        assert res.json()["tunnel"]["on"] and json.loads(net.read_text()) == {"lan": False, "tunnel": True}
        tunnel.write_text(json.dumps({"status": "running", "url": "https://x.trycloudflare.com", "error": None}))
        client.put("/api/v1/network", json={"lan": True}, headers=AUTH)
        assert json.loads(net.read_text()) == {"lan": True, "tunnel": True}
        view = client.get("/api/v1/network", headers=AUTH).json()
        assert view["lan"]["on"] and not view["lan"]["active"]  # until the app restarts the host
        assert view["tunnel"]["url"] == "https://x.trycloudflare.com"
        # Only from this computer, not over the LAN or the tunnel.
        assert client.put("/api/v1/network", json={"lan": False}, headers={**AUTH, "Host": "x.trycloudflare.com"}).status_code == 400
        lan = TestClient(app, client=("192.168.1.20", 50000))  # even when it says Host: 127.0.0.1
        assert lan.put("/api/v1/network", json={"lan": False}, headers={**AUTH, "Host": "testserver"}).status_code == 400

        app.state.settings.serve_on_network = True
        url = client.get("/api/v1/network", headers=AUTH).json()["lan"]["url"]
        assert url is None or url.endswith(":7788")  # None when the test machine is offline

    app, _, _ = _setup_test_app(tmp_path)
    with TestClient(app) as client:  # a host run on its own has nothing to share through
        assert client.get("/api/v1/network", headers=AUTH).json()["error"]["code"] == "not_found"


def test_extra_keys_scopes_and_daily_cap(tmp_path: Path) -> None:
    keys_file = tmp_path / "keys.json"
    app, fake, _ = _setup_test_app(tmp_path, keys_file=keys_file, allowed_hosts=["testserver"])
    body = {"prompt": "a fox", "response_format": "b64_json"}
    with TestClient(app, client=LOCAL) as client:
        app.state.providers["comfyui"].client = fake
        res = client.post("/api/v1/keys", json={"name": "Phone", "daily_cap": 1}, headers=AUTH)
        assert res.status_code == 201
        made = res.json()
        token, key_id = made.pop("key"), made["id"]
        assert made["scopes"] == ["local"] and "hash" not in made
        assert token not in keys_file.read_text()  # only its hash is kept
        assert Keys(keys_file, "main").find(token).id == key_id  # and it survives a restart
        phone = {"X-API-Key": token}

        assert client.post("/v1/images/generations", json=body, headers=phone).status_code == 200
        res = client.post("/v1/images/generations", json=body, headers=phone)
        assert res.status_code == 429 and res.json()["error"]["code"] == "daily_cap"
        assert client.post("/v1/images/generations", json=body, headers=AUTH).status_code == 200  # no cap
        [listed] = client.get("/api/v1/keys", headers=AUTH).json()["keys"]
        assert listed["used_24h"] == 1 and listed["last_used"]

        # Another key makes images and nothing else, and sees only its own jobs.
        for path in ["/api/v1/keys", "/api/v1/stats", "/api/v1/system", "/api/v1/local-models"]:
            assert client.get(path, headers=phone).json()["error"]["code"] == "forbidden"
        assert client.delete("/api/v1/styles/cartoon", headers=phone).status_code == 403
        job = client.post("/api/v1/jobs", json=body, headers=AUTH).json()["id"]
        assert client.get(f"/api/v1/jobs/{job}", headers=phone).status_code == 404

        # No cloud scope, no cloud models.
        client.put(f"/api/v1/keys/{key_id}", json={"name": "Phone", "daily_cap": None}, headers=AUTH)
        model = app.state.models["z-image-turbo"]
        app.state.models["z-image-turbo"] = model.model_copy(update={"cloud": True})
        res = client.post("/v1/images/generations", json=body, headers=phone)
        assert res.status_code == 403 and res.json()["error"]["code"] == "forbidden"
        app.state.models["z-image-turbo"] = model
        assert client.post("/v1/images/generations", json=body, headers=phone).status_code == 200  # cap lifted

        # Keys are managed only from this computer; then a revoked key is refused.
        lan = TestClient(app, client=("192.168.1.20", 50000))
        assert lan.post("/api/v1/keys", json={"name": "x"}, headers=AUTH).status_code == 400
        assert client.delete(f"/api/v1/keys/{key_id}", headers=AUTH).status_code == 204
        assert client.post("/v1/images/generations", json=body, headers=phone).status_code == 403


def test_stats_and_system(tmp_path: Path) -> None:
    hardware = tmp_path / "hardware.json"
    hardware.write_text(json.dumps(hw("mps", 14.4, 24, 0)))
    app, fake, _ = _setup_test_app(tmp_path, hardware_file=hardware, models_dir=tmp_path / "models")
    with TestClient(app) as client:
        app.state.providers["comfyui"].client = fake
        for style in ["cartoon", None]:
            body = {"prompt": "a fox", "response_format": "b64_json", **({"style": style} if style else {})}
            assert client.post("/v1/images/generations", json=body, headers=AUTH).status_code == 200
        fake.generate_error = ApiError("workflow_failed", "boom")
        client.post("/v1/images/generations", json={"prompt": "a fox"}, headers=AUTH)

        stats = client.get("/api/v1/stats?days=7", headers=AUTH).json()
        assert stats["totals"] == {"jobs": 3, "done": 2, "failed": 1, "cancelled": 0, "local": 3}
        assert stats["errors"] == [{"code": "workflow_failed", "count": 1}]
        assert stats["styles"] == [{"style": "cartoon", "count": 1}]
        assert stats["models"][0]["jobs"] == 3 and stats["models"][0]["p50_ms"] is not None
        assert len(stats["days"]) == 7 and stats["days"][-1]["done"] == 2

        system = client.get("/api/v1/system", headers=AUTH).json()
        assert system["engine"]["up"] and system["queue"] == {"running": 0, "queued": 0}
        assert system["hardware"]["backend"] == "mps"

        local = client.get("/api/v1/local-models", headers=AUTH).json()
        assert local["models"][0]["id"] == "z-image-turbo"
        assert client.get("/api/v1/local-models").status_code == 401
        assert client.post("/api/v1/local-models/nope/download", headers=AUTH).status_code == 404


def test_settings_parse_allowed_hosts() -> None:
    assert Settings(allowed_hosts="127.0.0.1, localhost").allowed_hosts == ["127.0.0.1", "localhost"]
