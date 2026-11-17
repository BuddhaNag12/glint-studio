"""A local benchmark: how fast each installed model makes images on this computer.

Each run goes through the job queue like any other image, so it never fights a user's job for the
GPU. Results go to bench.json beside engine.json, and tune the speed labels on the Models page.
"""

from __future__ import annotations

import asyncio
import json
import math
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field

from app.auth import require_main_key
from app.catalog import CatalogModel, ResolvedParams, check_params, resolve_params
from app.errors import ApiError
from app.jobs import JobQueue
from app.local_models import model_installed
from app.model_folders import model_roots
from app.providers.base import ProviderRequest
from app.styles import compose

PROMPT = "a lighthouse on a rocky coast at sunset, waves, detailed, photographic"
SEED = 1234  # the same image every run, so runs compare
KEEP = 200  # rows kept in bench.json
VRAM_POLL_SECONDS = 0.5
Size = tuple[int, int]


def bench_file(where: Any) -> Path | None:
    """bench.json beside engine.json. `where` is the Settings, or the file's path itself."""
    if where is None or isinstance(where, Path):
        return where
    return where.engine_file.with_name("bench.json") if where.engine_file else None


def load_results(path: Path | None) -> list[dict]:
    try:
        data = json.loads(path.read_text()) if path else {}
    except (OSError, ValueError):
        return []
    rows = data.get("results") if isinstance(data, dict) else None
    return [r for r in rows if isinstance(r, dict)] if isinstance(rows, list) else []


def save_results(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps({"results": rows[-KEEP:]}, indent=1))
    os.replace(tmp, path)  # never half a file


def measured(where: Any, model_id: str) -> dict | None:
    """The latest benchmark speed for a model on this computer, or None if it was never measured.

    `where` is the Settings (bench.json sits beside its engine_file) or the bench.json path.
    From the model's latest benchmark run, the largest size that finished, as
    {"s_per_image": float, "size": "1024x1024", "width", "height", "steps", "s_per_step": float | None,
     "peak_vram": bytes | None, "date": unix seconds}.
    """
    rows = [r for r in load_results(bench_file(where))
            if r.get("model") == model_id and r.get("s_per_image") is not None and not r.get("largest")]
    if not rows:
        return None
    latest = max(r.get("run", 0) for r in rows)
    best = max((r for r in rows if r.get("run", 0) == latest), key=lambda r: r["width"] * r["height"])
    return {**best, "size": f"{best['width']}x{best['height']}"}


def average(values: list[float | None], runs: int) -> float | None:
    """The mean, without the first run when there were several: it carries the model load."""
    kept = [v for v in (values[1:] if runs > 1 else values) if v is not None]
    return round(sum(kept) / len(kept), 3) if kept else None


def _fits(model: CatalogModel, size: Size) -> bool:
    try:
        check_params(model, ResolvedParams(size[0], size[1], None, None, 1))
        return True
    except ApiError:
        return False


def _area(size: Size) -> int:
    return size[0] * size[1]


def _max_square(model: CatalogModel) -> Size:
    lim = model.limits
    if not lim.max_pixels:
        return max(lim.sizes.values(), key=_area)
    side = int(math.isqrt(lim.max_pixels)) // lim.multiple_of * lim.multiple_of
    return side, side


def size_options(model: CatalogModel) -> list[Size]:
    """What the page offers: 512², 768², 1024² and the largest square the limits allow, or the named sizes."""
    if not model.limits.custom_size:
        return sorted(set(model.limits.sizes.values()), key=_area)
    picks = {(512, 512), (768, 768), (1024, 1024), _max_square(model)}
    return sorted((s for s in picks if _fits(model, s)), key=_area)


def fit_ladder(model: CatalogModel) -> list[Size]:
    """Sizes the "largest that fits" test tries, smallest first, up to the catalog's limit."""
    if not model.limits.custom_size:
        return size_options(model)
    top = _max_square(model)
    sides = {*range(512, top[0], 128), top[0]}
    return sorted((s for s in ((n, n) for n in sides) if _fits(model, s)), key=_area)


@dataclass
class _Sample:
    seconds: float | None = None
    s_per_step: float | None = None
    peak_vram: int | None = None
    error: ApiError | None = None


@dataclass
class _Run:
    model: CatalogModel
    sizes: list[Size]
    steps: int | None
    runs: int
    fits: bool
    id: int = field(default_factory=lambda: int(time.time() * 1000))
    status: str = "running"  # running | done | failed | cancelled
    done: int = 0
    total: int = 0
    current: str | None = None
    job: Any = None
    rows: list[dict] = field(default_factory=list)
    error: str | None = None
    task: asyncio.Task | None = None


class Bench:
    def __init__(self, jobs: JobQueue, provider: Any, path: Path | None) -> None:
        self.jobs = jobs
        self.provider = provider
        self.path = path
        self.run: _Run | None = None
        self._memory: list[dict] = []  # results when there's no app data folder

    def results(self) -> list[dict]:
        return load_results(self.path) if self.path else list(self._memory)

    def view(self) -> dict | None:
        r = self.run
        if r is None:
            return None
        part = r.job.progress if r.job is not None and r.status == "running" else 0.0
        return {
            "id": r.id, "status": r.status, "model": r.model.id, "current": r.current,
            "done": r.done, "total": r.total, "progress": round(min(1.0, (r.done + part) / max(1, r.total)), 3),
            "rows": r.rows, "error": r.error,
        }

    def start(self, model: CatalogModel, sizes: list[Size], steps: int | None, runs: int, fits: bool) -> None:
        if self.run and self.run.status == "running":
            raise ApiError("bad_params", "A benchmark is already running.")
        if not sizes and not fits:
            raise ApiError("bad_params", "Pick a size, or the largest-size test.")
        steps = steps if steps is not None else model.defaults.steps
        for w, h in sizes:
            resolve_params(model, width=w, height=h, steps=steps)  # rejects what the model can't take
        run = _Run(model, sorted(set(sizes), key=_area), steps, runs, fits)
        run.total = len(run.sizes) * runs + (len(fit_ladder(model)) if fits else 0)  # the ladder may stop early
        self.run = run
        run.task = asyncio.create_task(self._drive(run))

    def cancel(self) -> None:
        if self.run and self.run.task and not self.run.task.done():
            self.run.task.cancel()

    async def _drive(self, run: _Run) -> None:
        try:
            worked: Size | None = None
            for size in run.sizes:
                samples = []
                for _ in range(run.runs):
                    sample = await self._once(run, size)
                    run.done += 1
                    samples.append(sample)
                    if sample.error:
                        run.done += run.runs - len(samples)
                        break
                self._add(run, size, samples)
                if not samples[-1].error:
                    worked = size
            if run.fits:
                ladder = [s for s in fit_ladder(run.model) if worked is None or _area(s) > _area(worked)]
                run.total = run.done + len(ladder)
                last: _Sample | None = None
                for size in ladder:
                    sample = await self._once(run, size)
                    run.done += 1
                    if sample.error:
                        break  # the first size that fails ends the search
                    worked, last = size, sample
                if worked is not None:
                    self._add(run, worked, [last] if last else [], largest=True)
            run.status = "done"
        except asyncio.CancelledError:
            run.status = "cancelled"
        except Exception as exc:  # noqa: BLE001 - shown on the page
            run.status, run.error = "failed", str(exc) or type(exc).__name__
        finally:
            run.current, run.job = None, None
            if run.rows:
                try:
                    self._save(run.rows)
                except OSError as exc:
                    run.error = f"Couldn't save the results: {exc}"

    async def _once(self, run: _Run, size: Size) -> _Sample:
        """One image through the job queue; times it, and polls the engine's memory while it runs."""
        run.current = f"{size[0]}x{size[1]}"
        params = resolve_params(run.model, width=size[0], height=size[1], steps=run.steps)
        request = ProviderRequest(run.model, compose(run.model, PROMPT), params, SEED)
        marks: list[tuple[float, float]] = []
        peak: list[int] = []

        async def go(on_progress, on_preview):
            def progress(value: float) -> None:
                marks.append((time.monotonic(), value))
                on_progress(value)

            poller = asyncio.create_task(self._poll_vram(peak))
            try:
                return await self.provider.generate(request, progress, None)
            finally:
                poller.cancel()

        run.job = self.jobs.submit(go, model=run.model.id, prompt="Benchmark", response_format="b64_json", local=True)
        try:
            await self.jobs.wait(run.job)
        except ApiError as exc:
            if run.job.status == "cancelled":  # stopped from elsewhere (the job's cancel, or shutdown): stop the bench too
                raise asyncio.CancelledError from exc
            return _Sample(error=exc)
        except Exception as exc:  # noqa: BLE001 - jobs.wait re-raises what the provider raised
            return _Sample(error=ApiError("workflow_failed", str(exc) or type(exc).__name__))
        return _Sample(run.job.elapsed_ms / 1000, _per_step(marks, params.steps), max(peak) if peak else None)

    async def _poll_vram(self, peak: list[int]) -> None:
        # Memory in use on the engine's devices: other apps' use counts too, and on Apple Silicon it's system memory.
        stats = getattr(self.provider, "system_stats", None)
        if stats is None:
            return
        while True:
            try:
                devices = (await stats()).get("devices") or []
                used = sum(d["vram_total"] - d["vram_free"] for d in devices if d.get("vram_total") is not None)
                if devices:
                    peak.append(used)
            except (ApiError, KeyError, TypeError):
                return
            await asyncio.sleep(VRAM_POLL_SECONDS)

    def _add(self, run: _Run, size: Size, samples: list[_Sample], largest: bool = False) -> None:
        err = next((s.error for s in samples if s.error), None)
        good = [s for s in samples if not s.error]
        n = len(samples) if largest else run.runs
        run.rows.append({
            "run": run.id, "date": int(time.time()), "model": run.model.id, "name": run.model.name,
            "width": size[0], "height": size[1], "steps": run.steps, "runs": len(samples), "largest": largest,
            "s_per_image": average([s.seconds for s in good], n) if good else None,
            "s_per_step": average([s.s_per_step for s in good], n) if good else None,
            "peak_vram": max((s.peak_vram for s in good if s.peak_vram is not None), default=None),
            "error": {"code": err.code, "message": err.message} if err else None,
        })

    def _save(self, rows: list[dict]) -> None:
        if self.path is None:
            self._memory = (self._memory + rows)[-KEEP:]
        else:
            save_results(self.path, load_results(self.path) + rows)


def _per_step(marks: list[tuple[float, float]], steps: int | None) -> float | None:
    """From the sampler's progress events: time between the first and last, over the steps between them."""
    if not steps or len(marks) < 2:
        return None
    (t0, v0), (t1, v1) = marks[0], marks[-1]
    return (t1 - t0) / ((v1 - v0) * steps) if v1 > v0 else None


# --- routes ---

router = APIRouter()


def _bench(request: Request) -> Bench:
    state = request.app.state
    if getattr(state, "bench", None) is None:
        state.bench = Bench(state.jobs, state.providers["comfyui"], bench_file(state.settings))
    return state.bench


def _installed(request: Request) -> list[CatalogModel]:
    cfg = request.app.state.settings
    roots = model_roots(cfg.models_dir, cfg.engine_file)
    return [m for m in request.app.state.models.values()
            if m.variants and not m.cloud and m.provider == "comfyui" and model_installed(roots, m)]


class BenchRequest(BaseModel):
    model: str
    sizes: list[tuple[int, int]] = Field(default_factory=list, max_length=16)
    steps: int | None = None  # None: the model's default
    runs: int = Field(default=2, ge=1, le=10)  # the first is a warm-up when there are several
    fits: bool = False  # also look for the largest size that fits


@router.get("/api/v1/bench", dependencies=[Depends(require_main_key)])
async def bench_status(request: Request) -> dict:
    """The run in progress (or the last one), past results, and the models it can run."""
    bench = _bench(request)
    models = await asyncio.to_thread(_installed, request)
    return {
        "run": bench.view(),
        "results": await asyncio.to_thread(bench.results),
        "models": [
            {"id": m.id, "name": m.name, "steps": m.defaults.steps, "step_limits": m.limits.steps,
             "sizes": size_options(m)}
            for m in models
        ],
    }


@router.post("/api/v1/bench", status_code=202, dependencies=[Depends(require_main_key)])
async def start_bench(body: BenchRequest, request: Request) -> dict:
    model = next((m for m in await asyncio.to_thread(_installed, request) if m.id == body.model), None)
    if model is None:
        raise ApiError("model_missing", f"'{body.model}' isn't a downloaded local model.")
    bench = _bench(request)
    bench.start(model, body.sizes, body.steps, body.runs, body.fits)
    return bench.view() or {}


@router.delete("/api/v1/bench", dependencies=[Depends(require_main_key)])
async def cancel_bench(request: Request) -> dict:
    bench = _bench(request)
    bench.cancel()
    await asyncio.sleep(0)  # let the run see the cancel before answering
    return bench.view() or {}
