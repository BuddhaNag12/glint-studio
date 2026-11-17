"""Keeps a model in memory: loads one when the engine starts, unloads it on request or after it sits idle.

ComfyUI loads a model on the first prompt that needs it (a minute or two for a large one) and keeps
it until told to free it. Loading = a tiny warm-up prompt run outside the job queue.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from pathlib import Path

from app.catalog import CatalogModel, ResolvedParams
from app.comfy_client import ComfyConnectionError
from app.errors import ApiError
from app.jobs import Job
from app.local_models import model_installed
from app.providers.base import ProviderRequest
from app.providers.comfyui import ComfyUIProvider
from app.styles import ComposedPrompt

logger = logging.getLogger(__name__)


class Engine:
    def __init__(
        self,
        provider: ComfyUIProvider,
        models: dict[str, CatalogModel],
        roots: Callable[[], list[Path]],
        preload: Callable[[], bool],
        default_model: str,
        idle_minutes: Callable[[str], float] = lambda _model: 0,
        busy: Callable[[], bool] = lambda: False,
    ) -> None:
        self.provider = provider
        self.models = models
        self.roots = roots  # the models folder and the linked ones, read when needed
        self.preload = preload
        self.default_model = default_model
        self.idle_minutes = idle_minutes  # 0 = never; read every tick so a change applies at once
        self.busy = busy  # jobs queued or running: never unload under them
        self.status = "idle"  # idle | loading | loaded | failed
        self.model: str | None = None
        self.error: str | None = None
        self._task: asyncio.Task | None = None
        self.last_used = time.monotonic()
        self.started = time.time()  # the host
        self.up_since: float | None = None  # the engine, unix seconds; None while it's down

    def view(self) -> dict:
        return {"status": self.status, "model": self.model, "error": self.error}

    def _local(self, model_id: str | None) -> CatalogModel | None:
        model = self.models.get(model_id or "")
        return model if model and model.variants and not model.cloud and model.provider == "comfyui" else None

    def pick(self) -> CatalogModel | None:
        """The default model if it's installed, else the first installed local one."""
        roots = self.roots()
        ids = [self.default_model, *(m for m in self.models if m != self.default_model)]
        return next((m for i in ids if (m := self._local(i)) and model_installed(roots, m)), None)

    def load(self, model_id: str | None = None) -> None:
        model = self._local(model_id) if model_id else self.pick()
        if model is None:
            raise ApiError("model_missing", "No downloaded model to load." if not model_id else f"'{model_id}' isn't a local model.")
        if self._task and not self._task.done():
            self._task.cancel()
        self.status, self.model, self.error = "loading", model.id, None
        self._task = asyncio.create_task(self._warm(model))

    async def _warm(self, model: CatalogModel) -> None:
        cfg = model.defaults.cfg if model.defaults else None
        request = ProviderRequest(model, ComposedPrompt("a plain grey square", None, False), ResolvedParams(256, 256, 1, cfg, 1), seed=0)
        try:
            await self.provider.generate(request)
            self.status, self.last_used = "loaded", time.monotonic()
        except ApiError as exc:
            logger.warning("Loading %s failed: %s", model.id, exc.message)
            self.status, self.error = "failed", exc.message

    async def unload(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()  # stops the warm-up prompt in ComfyUI too
        try:
            await self.provider.client.free()
        except ComfyConnectionError as exc:
            raise ApiError("engine_down", str(exc)) from exc
        self.status, self.model, self.error = "idle", None, None

    def finished(self, job: Job) -> None:
        """A finished job leaves its model in memory."""
        if job.status == "done" and self._local(job.model):
            self.status, self.model, self.error = "loaded", job.model, None
            self.last_used = time.monotonic()

    def idle_due(self) -> bool:
        """A loaded model nobody has used for its idle limit; a job in the queue counts as use."""
        if self.status != "loaded" or not self.model:
            return False
        if self.busy():
            self.last_used = time.monotonic()
            return False
        minutes = self.idle_minutes(self.model)
        return minutes > 0 and time.monotonic() - self.last_used >= minutes * 60

    def reset(self) -> None:
        """The engine (re)started: nothing is in memory."""
        if self._task and not self._task.done():
            self._task.cancel()
        self.status, self.model, self.error = "idle", None, None

    async def watch(self, interval: float = 3.0) -> None:
        """Polls the engine; each time it comes up, starts from empty and preloads if asked."""
        up = False
        while True:
            try:
                await self.provider.system_stats()
                now = True
            except ApiError:
                now = False
            if now and not up:
                self.reset()
                self.up_since = time.time()
                if self.preload():
                    try:
                        self.load()
                    except ApiError:
                        pass  # nothing downloaded yet
            elif now and self.idle_due():
                logger.info("Unloading %s: idle for its limit", self.model)
                try:
                    await self.unload()
                except ApiError:
                    pass  # went down this moment; the next start begins empty anyway
            if not now:
                self.up_since = None
            up = now
            await asyncio.sleep(interval)

    async def close(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()
