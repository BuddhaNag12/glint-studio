"""The local benchmark: timing, cancel, the largest-size search, and saved results."""

from __future__ import annotations

import asyncio
from pathlib import Path

from fastapi.testclient import TestClient

from app.bench import Bench, average, fit_ladder, measured, size_options
from app.catalog import load_catalog
from app.config import Settings
from app.errors import ApiError
from app.jobs import JobQueue
from app.providers.base import ProviderResult
from tests.test_api import _setup_test_app

ZIT = load_catalog(Settings().catalog_dir)["z-image-turbo"]


class FakeProvider:
    def __init__(self, seconds=(0.0,), fail_above: int | None = None) -> None:
        self.seconds = list(seconds)  # per call; the last repeats
        self.fail_above = fail_above
        self.gate: asyncio.Event | None = None
        self.calls: list[tuple[int, int]] = []

    async def system_stats(self) -> dict:
        return {"devices": [{"vram_total": 100, "vram_free": 100 - 10 * len(self.calls)}]}

    async def generate(self, request, on_progress=None, on_preview=None) -> ProviderResult:
        p = request.params
        self.calls.append((p.width, p.height))
        if self.fail_above and p.width > self.fail_above:
            raise ApiError("workflow_failed", "CUDA out of memory")
        if self.gate:
            await self.gate.wait()
        wait = self.seconds[min(len(self.calls), len(self.seconds)) - 1]
        on_progress(1 / 6)
        await asyncio.sleep(wait)
        on_progress(1.0)
        return ProviderResult(images=[], seed=1, job_ref="x")


def _bench(provider: FakeProvider, path: Path | None = None) -> Bench:
    jobs = JobQueue(max_concurrent=1, queue_timeout_seconds=5, run_timeout_seconds=5, keep_seconds=60)
    return Bench(jobs, provider, path)


def test_average_leaves_out_the_warm_up() -> None:
    assert average([9.0, 1.0, 2.0], 3) == 1.5
    assert average([9.0], 1) == 9.0
    assert average([9.0, None], 2) is None


def test_sizes_stay_inside_the_catalog_limits() -> None:
    assert size_options(ZIT) == [(512, 512), (768, 768), (1024, 1024), (1440, 1440)]
    ladder = fit_ladder(ZIT)
    assert ladder[0] == (512, 512) and ladder[-1] == (1440, 1440)
    assert all(w * h <= ZIT.limits.max_pixels for w, h in ladder)


async def test_a_run_times_each_size_without_the_warm_up(tmp_path: Path) -> None:
    provider = FakeProvider(seconds=(0.3, 0.02))
    bench = _bench(provider, tmp_path / "bench.json")
    bench.start(ZIT, [(512, 512)], None, 3, False)
    await bench.run.task
    view = bench.view()
    assert view["status"] == "done" and view["done"] == view["total"] == 3 and view["progress"] == 1.0
    [row] = view["rows"]
    assert row["steps"] == ZIT.defaults.steps and row["runs"] == 3
    assert row["s_per_image"] < 0.2  # the 0.3 s warm-up is left out
    assert row["s_per_step"] is not None and row["s_per_step"] < row["s_per_image"]
    assert row["peak_vram"] and row["error"] is None


async def test_cancel_stops_the_run_and_its_job() -> None:
    provider = FakeProvider()
    provider.gate = asyncio.Event()
    bench = _bench(provider)
    bench.start(ZIT, [(512, 512), (768, 768)], None, 2, False)
    await asyncio.sleep(0.05)
    job = bench.run.job
    bench.cancel()
    await asyncio.gather(bench.run.task, return_exceptions=True)
    assert bench.view()["status"] == "cancelled"
    assert job.status == "cancelled" and provider.calls == [(512, 512)]


async def test_the_largest_size_search_stops_at_the_first_failure() -> None:
    provider = FakeProvider(fail_above=1024)
    bench = _bench(provider)
    bench.start(ZIT, [], None, 2, True)
    await bench.run.task
    assert provider.calls[-2:] == [(1024, 1024), (1152, 1152)]  # nothing tried after the failure
    [row] = bench.view()["rows"]
    assert row["largest"] and (row["width"], row["height"]) == (1024, 1024)


async def test_results_are_saved_and_read_back(tmp_path: Path) -> None:
    path = tmp_path / "bench.json"
    bench = _bench(FakeProvider(), path)
    bench.start(ZIT, [(512, 512), (1024, 1024)], 4, 2, False)
    await bench.run.task
    assert len(_bench(FakeProvider(), path).results()) == 2
    best = measured(path, ZIT.id)
    assert best["size"] == "1024x1024"
    assert best["s_per_image"] is not None and best["steps"] == 4
    assert measured(Settings(engine_file=tmp_path / "engine.json"), ZIT.id) == best
    assert measured(path, "other") is None and measured(tmp_path / "none.json", ZIT.id) is None


def test_the_bench_page_data_needs_the_main_key(tmp_path: Path) -> None:
    app, _, _ = _setup_test_app(tmp_path, models_dir=tmp_path / "models")
    with TestClient(app) as client:
        assert client.get("/api/v1/bench").status_code == 401
        body = client.get("/api/v1/bench", headers={"Authorization": "Bearer test-secret-key"}).json()
        assert body == {"run": None, "results": [], "models": []}  # nothing downloaded
        res = client.post("/api/v1/bench", json={"model": ZIT.id}, headers={"Authorization": "Bearer test-secret-key"})
        assert res.status_code == 409
