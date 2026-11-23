"""Local models: which ones this machine can run, and downloading the chosen variant.

The desktop app scans the machine and writes the result to HARDWARE_FILE; `recommend` is pure so
it can be tested with fixture profiles. Downloads go to `<dest>.part`, resume after an
interruption, and are renamed into place only once the sha256 matches the catalog.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import shutil
from pathlib import Path
from collections.abc import Callable
from typing import Any

import httpx

from app.catalog import CatalogModel, Variant
from app.errors import ApiError
from app import net
from app.model_folders import file_present, get_setting

logger = logging.getLogger(__name__)

GB = 1e9
DISK_HEADROOM = 1.2  # room for the .part file and the OS on top of the download itself
RAM_SHARE = 0.75  # share of system RAM a model can use when it runs from RAM instead of GPU memory
FIT_ORDER = {"great": 0, "slow": 1, "wont_fit": 2}
FASTER = 1.5  # this much quicker than the best pick to count as "Faster, lighter"


def _gb(value: float) -> str:
    return f"{value:.0f} GB"


def _variant_bytes(variant: Variant) -> int:
    return sum(f.bytes for f in variant.files)


def _pick(hw: dict, variants: list[Variant]) -> tuple[Variant, bool]:
    """Variants are listed best first; the first that fits the GPU, else the smallest."""
    if hw["backend"] != "cpu":
        for variant in variants:
            if variant.min_vram_gb <= hw["vram_budget_gb"]:
                return variant, True
    return variants[-1], False


def _short_of_disk(hw: dict, size: int, installed: bool) -> bool:
    return not installed and size * DISK_HEADROOM > hw["disk_free_gb"] * GB


def speed_cost(model: CatalogModel, variant: Variant) -> float:
    """Relative time per image at the model's defaults: the weights read each step × steps × pixels.
    Only the ratio between two models on one machine means anything; it's an estimate."""
    w, h = model.limits.sizes[model.defaults.shape]
    weights = sum(f.bytes for f in variant.files if f.dest.startswith(("diffusion_models/", "checkpoints/")))
    return (weights or _variant_bytes(variant)) * (model.defaults.steps or 1) * w * h


def memory_split(hw: dict) -> dict:
    """The Models page's memory bar, from the same budget recommend_one measures against.
    Headroom is what a raised memory guard could still hand to models."""
    unified = hw["backend"] in ("mps", "cpu")  # models share system memory with the OS
    vram = max((g.get("vram_gb") or 0.0 for g in hw.get("gpus") or []), default=0.0)
    total = hw["ram_gb"] if unified else max(vram, hw["vram_budget_gb"])
    ceiling = total * RAM_SHARE if unified else total
    models = ceiling if hw["backend"] == "cpu" else min(hw["vram_budget_gb"], total)
    headroom = max(ceiling - models, 0.0)
    return {
        "unified": unified,
        "total_gb": total,
        "models_gb": models,
        "headroom_gb": headroom,
        "system_gb": max(total - models - headroom, 0.0),
    }


def recommend_one(hw: dict, model: CatalogModel, installed: bool) -> dict:
    variant, on_gpu = _pick(hw, model.variants)
    size = _variant_bytes(variant)
    if _short_of_disk(hw, size, installed):
        fit, reason = "wont_fit", f"Needs {_gb(size * DISK_HEADROOM / GB)} of free disk; you have {_gb(hw['disk_free_gb'])}."
    elif on_gpu:
        fit, reason = "great", f"Fits in your GPU's {_gb(hw['vram_budget_gb'])} of memory."
    elif variant.min_vram_gb <= hw["ram_gb"] * RAM_SHARE:
        fit = "slow"
        reason = (
            "No supported GPU, so it runs on the CPU: minutes per image."
            if hw["backend"] == "cpu"
            else f"Needs {_gb(variant.min_vram_gb)} of GPU memory and you have {_gb(hw['vram_budget_gb'])}, "
            "so it runs partly from system memory: slow."
        )
    else:
        fit, reason = "wont_fit", f"Needs {_gb(variant.min_vram_gb)} of memory; this machine has {_gb(hw['ram_gb'])}."
    return {
        "id": model.id,
        "name": model.name,
        "license": model.license.model_dump() if model.license else None,
        "fit": fit,
        "reason": reason,
        "precision": variant.precision,
        "min_vram_gb": variant.min_vram_gb,
        "bytes": size,
        "installed": installed,
        "quality_rank": model.quality_rank,
        "cost": speed_cost(model, variant),
        "description": model.description,
        "categories": model.categories,
        "abilities": {  # what Discover shows on each row
            "steps": model.defaults.steps,
            "negative": model.limits.supports_negative,
            "seed": model.limits.supports_seed,
            "custom_size": model.limits.custom_size,
            "max_pixels": model.limits.max_pixels,
        },
    }


def _times(x: float) -> str:
    return f"{x:.0f}×" if x >= 2 else f"{x:.1f}×"


def add_tiers(rows: list[dict]) -> list[dict]:
    """Sorts rows into the Discover tiers. The best pick is the first that runs (rows come best
    first); "best" holds it and others like it, "faster" the ones much quicker than it, "better" the
    higher-quality ones that run slower, and "other" the rest. Speed is measured when both models
    were benchmarked (s_per_image), else estimated from speed_cost."""
    pick = next((r for r in rows if r["fit"] != "wont_fit"), None)
    for r in rows:
        r["tier"], r["speed_note"] = "other", None
        if pick is None or r["fit"] == "wont_fit":
            continue
        if r["fit"] != pick["fit"]:
            if r["quality_rank"] > pick["quality_rank"]:
                r["tier"] = "better"
            continue
        measured = bool(r.get("s_per_image") and pick.get("s_per_image"))
        ratio = pick["s_per_image"] / r["s_per_image"] if measured else pick["cost"] / r["cost"]
        if r is not pick and ratio >= FASTER:
            r["tier"] = "faster"
            r["speed_note"] = f"{_times(ratio)} faster than {pick['name']}" + ("" if measured else " (estimate)")
        else:
            r["tier"] = "best"
    return rows


def recommend(
    hw: dict,
    models: list[CatalogModel],
    installed=lambda _variant: False,
    measured: Callable[[str], float | None] = lambda _id: None,
) -> list[dict]:
    """Every local model, best fit first, then by catalog quality, sorted into tiers."""
    local = [m for m in models if m.variants]
    rows = [(m, recommend_one(hw, m, installed(_pick(hw, m.variants)[0]))) for m in local]
    rows.sort(key=lambda row: (FIT_ORDER[row[1]["fit"]], -row[0].quality_rank))
    for _m, r in rows:
        r["s_per_image"] = measured(r["id"])
    return add_tiers([r for _m, r in rows])


def unlockable(hw: dict, rows: list[dict]) -> list[str]:
    """Models a raised memory guard would let run on the GPU: more than today's budget, within the headroom."""
    if hw["backend"] == "cpu":
        return []
    split = memory_split(hw)
    ceiling = split["models_gb"] + split["headroom_gb"]
    return [
        r["id"]
        for r in rows
        if r["fit"] != "great"
        and hw["vram_budget_gb"] < r["min_vram_gb"] <= ceiling
        and not _short_of_disk(hw, r["bytes"], r["installed"])
    ]


def measured_speed(settings: Any, model_id: str) -> float | None:
    """Seconds per image from a benchmark run, when there is one."""
    try:
        from app.bench import measured  # optional: the benchmark feature may not be installed
    except ImportError:
        return None
    try:
        result = measured(settings, model_id)
    except Exception:  # a broken results file shouldn't hide the Models page
        logger.exception("Couldn't read the benchmark for %s", model_id)
        return None
    value = result.get("s_per_image") if isinstance(result, dict) else None
    return float(value) if isinstance(value, (int, float)) and value > 0 else None


def chosen_variant(hw: dict, model: CatalogModel) -> Variant | None:
    return _pick(hw, model.variants)[0] if model.variants else None


def runnable_variant(hw: dict, model: CatalogModel, roots: list[Path]) -> Variant | None:
    """The downloaded variant to run: the best that fits this machine, else the smallest downloaded."""
    have = [v for v in model.variants if variant_installed(roots, v)]
    return _pick(hw, have)[0] if have else None


def variant_installed(roots: list[Path], variant: Variant) -> bool:
    """Every file at full size in the models folder or a linked one. Downloads check the sha256
    before the rename, so full size = verified; a linked folder's files are taken on name and size."""
    return all(file_present(roots, f.dest, f.bytes) for f in variant.files)


def model_installed(roots: list[Path], model: CatalogModel) -> bool:
    return any(variant_installed(roots, v) for v in model.variants)


def deletable_files(models_dir: Path, roots: list[Path], model: CatalogModel, others: list[CatalogModel]) -> list[Path]:
    """The model's files (and leftover .part files) in our own models folder, minus any file another
    installed model also uses. Linked folders belong to another ComfyUI and are never touched."""
    shared = {f.dest for m in others if m.id != model.id for v in m.variants if variant_installed(roots, v) for f in v.files}
    dests = sorted({f.dest for v in model.variants for f in v.files} - shared)
    return [p for d in dests for p in (models_dir / d, models_dir / (d + ".part")) if p.is_file()]


def load_hardware(hardware_file: Path | None, models_dir: Path, engine_file: Path | None = None) -> dict:
    """The app's scan, with free disk measured now (it changes as models download).
    On shared memory the Aggressive memory guard lets models use up to RAM_SHARE of it."""
    hw: dict[str, Any]
    if hardware_file is not None and hardware_file.is_file():
        hw = json.loads(hardware_file.read_text(encoding="utf-8"))
    else:
        # shortcut: a host run without the app has no GPU scan, so it recommends for the CPU;
        # read nvidia-smi here if people run the host on its own.
        ram = os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES") if hasattr(os, "sysconf") else 0
        hw = {"backend": "cpu", "vram_budget_gb": 0.0, "ram_gb": ram / 1024**3}
    if hw.get("backend") == "mps" and get_setting(engine_file, "memory_guard") == "aggressive":
        hw["vram_budget_gb"] = max(hw["vram_budget_gb"], hw["ram_gb"] * RAM_SHARE)
    models_dir.mkdir(parents=True, exist_ok=True)
    hw["disk_free_gb"] = shutil.disk_usage(models_dir).free / 1024**3
    return hw


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        while chunk := f.read(1 << 20):
            digest.update(chunk)
    return digest.hexdigest()


class Downloads:
    """One model download at a time, run as a background task; the dashboard polls `state`."""

    def __init__(self, client: httpx.AsyncClient, models_dir: Path, retries: int = 5, engine_file: Path | None = None) -> None:
        self.client = client
        self.engine_file = engine_file  # its "downloads" key: source, mirror, proxies (app/net.py)
        self.models_dir = models_dir
        self.retries = retries
        self.model_id: str | None = None
        self.state: dict[str, Any] = {}
        self._task: asyncio.Task | None = None

    @property
    def busy(self) -> bool:
        return self._task is not None and not self._task.done()

    def start(self, model_id: str, variant: Variant) -> None:
        if self.busy:
            raise ApiError("bad_params", f"'{self.model_id}' is downloading; wait for it or cancel it.")
        settings = net.read(self.engine_file)  # bad settings fail here, before anything starts
        self.model_id = model_id
        total = _variant_bytes(variant)
        self.state = {"status": "downloading", "done": 0, "total": total, "verifying": False, "error": None}
        self._task = asyncio.create_task(self._run(variant, settings))

    async def cancel(self) -> None:
        """Stops the download; the .part file stays and the next download resumes it."""
        if self.busy:
            assert self._task is not None
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)

    def view(self, model_id: str) -> dict | None:
        return dict(self.state) if model_id == self.model_id and self.state else None

    async def _run(self, variant: Variant, settings: net.DownloadSettings = net.DEFAULT) -> None:
        client = net.client(settings)
        try:
            await self.download_variant(variant, settings, client)
            self.state["status"] = "done"
        except asyncio.CancelledError:
            self.state["status"] = "cancelled"
        except Exception as exc:  # reported to the dashboard
            if not isinstance(exc, ApiError):
                logger.exception("Download of %s failed", self.model_id)
            error = getattr(exc, "message", None) or str(exc) or type(exc).__name__
            self.state.update(status="failed", error=error + net.hint(settings))
        finally:
            self.state["verifying"] = False
            if client is not None:
                await client.aclose()

    async def download_variant(
        self, variant: Variant, settings: net.DownloadSettings = net.DEFAULT, client: httpx.AsyncClient | None = None
    ) -> None:
        before = 0
        for f in variant.files:
            dest = self.models_dir / f.dest  # the catalog schema keeps dest to "<folder>/<name>"
            if dest.is_file() and dest.stat().st_size == f.bytes:
                before += f.bytes
                continue
            dest.parent.mkdir(parents=True, exist_ok=True)
            part = dest.with_name(dest.name + ".part")
            await self._fetch(net.file_url(f.url, settings), part, f.bytes, before, client)
            size = part.stat().st_size
            if size != f.bytes:
                part.unlink(missing_ok=True)
                raise ApiError("workflow_failed", f"{f.dest} came out at {size} bytes, expected {f.bytes}. Try again.")
            self.state.update(done=before + f.bytes, verifying=True)
            got = await asyncio.to_thread(_sha256, part)
            if got != f.sha256:
                part.unlink(missing_ok=True)
                raise ApiError("workflow_failed", f"{f.dest} is corrupt (sha256 {got}, expected {f.sha256}). Try again.")
            part.replace(dest)
            self.state["verifying"] = False
            before += f.bytes
        self.state["done"] = self.state["total"]

    # shortcut: no Hugging Face token, so gated models (requires_hf_token) can't download yet;
    # send one from the keychain when the catalog has a gated model.
    async def _fetch(self, url: str, part: Path, size: int, before: int, client: httpx.AsyncClient | None = None) -> None:
        for attempt in range(self.retries + 1):
            have = part.stat().st_size if part.exists() else 0
            if have >= size:
                return
            try:
                headers = {"Range": f"bytes={have}-"} if have else {}
                async with (client or self.client).stream("GET", url, headers=headers, follow_redirects=True, timeout=60) as resp:
                    if resp.status_code not in (200, 206):
                        raise ApiError("provider_down", f"The download server answered {resp.status_code}.")
                    # 200: the server ignored the range, so start over.
                    with part.open("ab" if resp.status_code == 206 else "wb") as out:
                        done = have if resp.status_code == 206 else 0
                        async for chunk in resp.aiter_bytes(1 << 20):
                            out.write(chunk)
                            done += len(chunk)
                            self.state["done"] = before + done
                return
            except httpx.TransportError as exc:
                if attempt == self.retries:
                    raise ApiError("provider_down", f"The download stopped: {exc}. Press Download to resume.") from exc
                await asyncio.sleep(min(2 * (attempt + 1), 10))
