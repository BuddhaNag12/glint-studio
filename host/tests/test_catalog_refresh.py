"""The online catalog: built in CI, checked and cached by the host, added to the model list."""

from __future__ import annotations

import asyncio
import json
import shutil
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from app import catalog_refresh
from app.catalog import CatalogError
from app.catalog_refresh import apply, bundle, load_cached
from tests.test_api import _setup_test_app

CATALOG = Path(__file__).resolve().parents[2] / "catalog"
BUILTIN = {p.stem for p in (CATALOG / "models").glob("*.json")}  # the models the app ships
AUTH = {"Authorization": "Bearer test-secret-key"}
LOCAL = ("127.0.0.1", 50000)


def _catalog_with_extra(tmp_path: Path) -> Path:
    """The real catalog plus "extra", a model with its own workflow."""
    root = tmp_path / "src"
    shutil.copytree(CATALOG, root)
    entry = json.loads((root / "models" / "z-image-turbo.json").read_text())
    entry.update(id="extra", name="Extra", workflow="extra-wf")
    (root / "models" / "extra.json").write_text(json.dumps(entry))
    for suffix in (".json", ".mapping.json"):
        shutil.copy(root / "workflows" / f"z-image-turbo{suffix}", root / "workflows" / f"extra-wf{suffix}")
    return root


def test_bundle_round_trip_keeps_only_new_models(tmp_path: Path) -> None:
    data = json.loads(json.dumps(bundle(_catalog_with_extra(tmp_path))))  # as it arrives over the network
    cache = tmp_path / "online-catalog"
    assert apply(data, BUILTIN, cache) == []
    assert sorted(p.name for p in (cache / "workflows").iterdir()) == ["extra-wf.json", "extra-wf.mapping.json"]
    assert list(load_cached(cache, BUILTIN)) == ["extra"]
    # The next refresh replaces the cache: a model dropped online is gone here too.
    data["models"] = [m for m in data["models"] if m["id"] != "extra"]
    apply(data, BUILTIN, cache)
    assert load_cached(cache, BUILTIN) == {}


def test_entries_this_app_cannot_use_are_skipped(tmp_path: Path) -> None:
    data = bundle(_catalog_with_extra(tmp_path))
    newer = {**data["models"][0], "id": "newer", "video": True}  # a key from a newer schema
    lost = {**data["models"][0], "id": "lost", "workflow": "nowhere"}
    escape = {**data["models"][0], "id": "escape", "workflow": "../x"}
    data["models"] += [newer, lost, escape, "junk"]
    cache = tmp_path / "online-catalog"
    assert apply(data, BUILTIN, cache) == ["newer", "lost", "escape", "None"]
    assert list(load_cached(cache, BUILTIN)) == ["extra"]
    with pytest.raises(CatalogError, match="newer version"):
        apply({**data, "schema": 2}, set(), cache)
    assert list(load_cached(cache, set())) == ["extra"]  # a failed refresh keeps the last good one


def test_refresh_adds_models_and_the_switch_stops_the_startup_check(tmp_path: Path, monkeypatch) -> None:
    data = bundle(_catalog_with_extra(tmp_path))
    calls: list[str] = []

    async def fake_fetch(_client, url: str):
        calls.append(url)
        return data

    monkeypatch.setattr(catalog_refresh, "fetch", fake_fetch)
    engine_file = tmp_path / "data" / "engine.json"
    engine_file.parent.mkdir()
    engine_file.write_text(json.dumps({"catalog_refresh": False}))
    settings = dict(engine_file=engine_file, catalog_url="https://example.test/catalog.json", allowed_hosts=["testserver"])
    app, _, _ = _setup_test_app(tmp_path, **settings)
    with TestClient(app, client=LOCAL) as client:
        assert client.get("/api/v1/catalog", headers=AUTH).json()["enabled"] is False
        assert calls == []  # off: no request at startup
        view = client.post("/api/v1/catalog/refresh", headers=AUTH).json()
        assert calls and view["added"] == ["extra"] and view["error"] is None
        assert "extra" in {m["id"] for m in client.get("/api/v1/models", headers=AUTH).json()["models"]}
        assert client.put("/api/v1/catalog", json={"enabled": True}, headers=AUTH).json()["enabled"] is True

    # The next start has "extra" from the cache before any check; then the check runs.
    calls.clear()
    app, _, _ = _setup_test_app(tmp_path, **settings)
    with TestClient(app, client=LOCAL) as client:
        assert "extra" in app.state.models
        provider = app.state.providers["comfyui"]
        assert provider.extra_workflows_dirs[0] == engine_file.parent / "online-catalog" / "workflows"
        client.get("/api/v1/catalog", headers=AUTH)
    assert calls == ["https://example.test/catalog.json"]


def test_a_failed_check_says_why_in_plain_words(tmp_path: Path) -> None:
    engine_file = tmp_path / "engine.json"
    engine_file.write_text("{}")

    async def check(url: str) -> str | None:
        status = lambda req: httpx.Response(404 if "missing" in str(req.url) else 500)  # noqa: E731
        async with httpx.AsyncClient(transport=httpx.MockTransport(status)) as client:
            refresh = catalog_refresh.CatalogRefresh(url, engine_file, {}, client)
            await refresh.refresh()
            return refresh.error

    assert asyncio.run(check("https://x.test/missing")) == "No new models are published yet."
    assert asyncio.run(check("https://x.test/broken")) == "The server answered 500."
