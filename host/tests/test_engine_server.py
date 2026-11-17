"""Idle unload, memory guard presets and the engine server endpoints."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.engine import Engine
from app.engine_server import idle_minutes, memory_flags, read_server_settings, restart_needed
from tests.test_api import _setup_test_app
from tests.test_engine import MODELS, ZIT, FakeProvider, _install

AUTH = {"Authorization": "Bearer test-secret-key"}
LOCAL = ("127.0.0.1", 50000)


def _engine(tmp_path: Path, provider: FakeProvider, minutes: float, busy: list[bool]) -> Engine:
    return Engine(  # type: ignore[arg-type]
        provider, MODELS, lambda: [tmp_path], lambda: True, "z-image-turbo", idle_minutes=lambda _m: minutes, busy=lambda: busy[0]
    )


@pytest.mark.asyncio
async def test_idle_model_is_unloaded_but_never_under_a_job(tmp_path: Path) -> None:
    _install(tmp_path, ZIT)
    provider, busy = FakeProvider(), [True]
    engine = _engine(tmp_path, provider, 0.001, busy)  # 60 ms
    watcher = asyncio.create_task(engine.watch(interval=0.01))
    await asyncio.sleep(0.15)
    assert engine.view()["status"] == "loaded" and provider.freed == 0  # a job is queued
    assert engine.up_since is not None
    busy[0] = False
    await asyncio.sleep(0.15)
    watcher.cancel()
    assert engine.view()["status"] == "idle" and provider.freed == 1


@pytest.mark.asyncio
async def test_idle_zero_keeps_the_model(tmp_path: Path) -> None:
    _install(tmp_path, ZIT)
    provider = FakeProvider()
    engine = _engine(tmp_path, provider, 0, [False])
    watcher = asyncio.create_task(engine.watch(interval=0.01))
    await asyncio.sleep(0.1)
    watcher.cancel()
    assert engine.view()["status"] == "loaded" and provider.freed == 0


def test_idle_minutes_per_model_override_and_bad_values(tmp_path: Path) -> None:
    f = tmp_path / "engine.json"
    assert idle_minutes(f, "a") == 0  # never, by default
    f.write_text(json.dumps({"idle_unload_minutes": 30, "idle_unload_by_model": {"a": 0, "b": "x", "c": 120}}))
    assert (idle_minutes(f, "a"), idle_minutes(f, "b"), idle_minutes(f, "c"), idle_minutes(f, "z")) == (0, 30, 120, 30)
    f.write_text(json.dumps({"idle_unload_minutes": True, "memory_guard": "huge", "reserve_vram_gb": -1}))
    s = read_server_settings(f)
    assert (idle_minutes(f, "a"), s["memory_guard"], s["reserve_vram_gb"]) == (0, "balanced", 2)


def test_memory_flags_and_restart_needed() -> None:
    assert memory_flags("balanced", 2) == []
    assert memory_flags("safe", 9) == ["--lowvram", "--reserve-vram", "2"]
    assert memory_flags("aggressive", 2) == ["--highvram"]
    assert memory_flags("custom", 1.5) == ["--reserve-vram", "1.5"]
    custom = {"memory_guard": "custom", "reserve_vram_gb": 3}
    argv = ["main.py", "--port", "1", "--reserve-vram", "3"]  # Rust writes 3.0 as "3"
    assert not restart_needed(custom, argv)
    assert restart_needed({"memory_guard": "balanced", "reserve_vram_gb": 2}, argv)
    assert not restart_needed(custom, None)  # engine down: unknown


def test_server_endpoints(tmp_path: Path) -> None:
    engine_file = tmp_path / "engine.json"
    app, fake, _ = _setup_test_app(tmp_path, engine_file=engine_file, allowed_hosts=["testserver"])
    with TestClient(app, client=LOCAL) as client:

        async def stats(timeout_seconds: float = 5.0) -> dict:
            return {"system": {"argv": ["main.py"], "comfyui_version": "0.45.0", "python_version": "3.12.4 (main)", "pytorch_version": "2.9"}}

        fake.check_health = stats
        app.state.providers["comfyui"].client = fake
        view = client.get("/api/v1/engine/server", headers=AUTH).json()
        assert view["status"] == "running" and view["restart_needed"] is False and view["has_model"] is False
        assert view["versions"]["python"] == "3.12.4"

        res = client.put("/api/v1/engine/server", json={"memory_guard": "aggressive", "idle_unload_minutes": 15}, headers=AUTH)
        assert res.json()["restart_needed"] is True
        assert json.loads(engine_file.read_text())["idle_unload_minutes"] == 15
        bad = client.put("/api/v1/engine/server", json={"idle_unload_by_model": {"nope": 5}}, headers=AUTH)
        assert bad.json()["error"]["code"] == "bad_params"

        res = client.post("/api/v1/engine/restart", headers=AUTH)
        assert res.status_code == 202 and (tmp_path / "engine_restart").is_file()
        assert res.json()["restart_needed"] is False  # pending: the app picks it up

        lan = TestClient(app, client=("192.168.1.20", 50000))
        assert lan.post("/api/v1/engine/restart", headers=AUTH).status_code == 400
        app.state.jobs.jobs["x"] = SimpleNamespace(status="running")
        assert client.post("/api/v1/engine/restart", headers=AUTH).json()["error"]["code"] == "bad_params"
        del app.state.jobs.jobs["x"]  # shutdown cancels real jobs only
