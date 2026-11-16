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
from typing import Any

import httpx

from app.catalog import CatalogModel, Variant
from app.errors import ApiError
from app.model_folders import file_present

logger = logging.getLogger(__name__)

GB = 1e9
DISK_HEADROOM = 1.2  # room for the .part file and the OS on top of the download itself
RAM_SHARE = 0.75  # share of system RAM a model can use when it runs from RAM instead of GPU memory
FIT_ORDER = {"great": 0, "slow": 1, "wont_fit": 2}


def _gb(value: float) -> str:
    return f"{value:.0f} GB"


def _variant_bytes(variant: Variant) -> int:
    return sum(f.bytes for f in variant.files)


def _pick(hw: dict, model: CatalogModel) -> tuple[Variant, bool]:
    """Variants are listed best first; the first that fits the GPU, else the smallest."""
    if hw["backend"] != "cpu":
        for variant in model.variants:
            if variant.min_vram_gb <= hw["vram_budget_gb"]:
                return variant, True
    return model.variants[-1], False


def recommend_one(hw: dict, model: CatalogModel, installed: bool) -> dict:
    variant, on_gpu = _pick(hw, model)
    size = _variant_bytes(variant)
    if not installed and size * DISK_HEADROOM > hw["disk_free_gb"] * GB:
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
    }


def recommend(hw: dict, models: list[CatalogModel], installed=lambda _variant: False) -> list[dict]:
    """Every local model, best fit first, then by catalog quality."""
    local = [m for m in models if m.variants]
    rows = [(m, recommend_one(hw, m, installed(_pick(hw, m)[0]))) for m in local]
    rows.sort(key=lambda row: (FIT_ORDER[row[1]["fit"]], -row[0].quality_rank))
    return [r for _m, r in rows]


def chosen_variant(hw: dict, model: CatalogModel) -> Variant | None:
    return _pick(hw, model)[0] if model.variants else None


def variant_installed(roots: list[Path], variant: Variant) -> bool:
    """Every file at full size in the models folder or a linked one. Downloads check the sha256
    before the rename, so full size = verified; a linked folder's files are taken on name and size."""
    return all(file_present(roots, f.dest, f.bytes) for f in variant.files)


def model_installed(roots: list[Path], model: CatalogModel) -> bool:
    return any(variant_installed(roots, v) for v in model.variants)


def load_hardware(hardware_file: Path | None, models_dir: Path) -> dict:
    """The app's scan, with free disk measured now (it changes as models download)."""
    hw: dict[str, Any]
    if hardware_file is not None and hardware_file.is_file():
        hw = json.loads(hardware_file.read_text(encoding="utf-8"))
    else:
        # shortcut: a host run without the app has no GPU scan, so it recommends for the CPU;
        # read nvidia-smi here if people run the host on its own.
        ram = os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES") if hasattr(os, "sysconf") else 0
        hw = {"backend": "cpu", "vram_budget_gb": 0.0, "ram_gb": ram / 1024**3}
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

    def __init__(self, client: httpx.AsyncClient, models_dir: Path, retries: int = 5) -> None:
        self.client = client
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
        self.model_id = model_id
        total = _variant_bytes(variant)
        self.state = {"status": "downloading", "done": 0, "total": total, "verifying": False, "error": None}
        self._task = asyncio.create_task(self._run(variant))

    async def cancel(self) -> None:
        """Stops the download; the .part file stays and the next download resumes it."""
        if self.busy:
            assert self._task is not None
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)

    def view(self, model_id: str) -> dict | None:
        return dict(self.state) if model_id == self.model_id and self.state else None

    async def _run(self, variant: Variant) -> None:
        try:
            await self.download_variant(variant)
            self.state["status"] = "done"
        except asyncio.CancelledError:
            self.state["status"] = "cancelled"
        except Exception as exc:  # reported to the dashboard
            if not isinstance(exc, ApiError):
                logger.exception("Download of %s failed", self.model_id)
            self.state.update(status="failed", error=getattr(exc, "message", None) or str(exc) or type(exc).__name__)
        finally:
            self.state["verifying"] = False

    async def download_variant(self, variant: Variant) -> None:
        before = 0
        for f in variant.files:
            dest = self.models_dir / f.dest  # the catalog schema keeps dest to "<folder>/<name>"
            if dest.is_file() and dest.stat().st_size == f.bytes:
                before += f.bytes
                continue
            dest.parent.mkdir(parents=True, exist_ok=True)
            part = dest.with_name(dest.name + ".part")
            await self._fetch(f.url, part, f.bytes, before)
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
    async def _fetch(self, url: str, part: Path, size: int, before: int) -> None:
        for attempt in range(self.retries + 1):
            have = part.stat().st_size if part.exists() else 0
            if have >= size:
                return
            try:
                headers = {"Range": f"bytes={have}-"} if have else {}
                async with self.client.stream("GET", url, headers=headers, follow_redirects=True, timeout=60) as resp:
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
