"""The job queue every generation runs through: async jobs, and the synchronous endpoints too.

Local jobs share the GPU, so at most MAX_CONCURRENT_JOBS run at once and the rest wait in order.
Cloud jobs start right away. Each job has two timeouts: how long it may wait for a slot, and how
long it may run once it has one.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Literal

from app.comfy_client import PreviewFn, ProgressFn
from app.errors import ApiError
from app.providers.base import ProviderResult

logger = logging.getLogger(__name__)

JobStatus = Literal["queued", "running", "done", "failed", "cancelled"]
Runner = Callable[[ProgressFn, PreviewFn], Awaitable[ProviderResult]]


@dataclass(eq=False)
class Job:
    id: str
    model: str
    prompt: str  # the user's prompt, before any style; used for titles and file names
    response_format: str
    local: bool
    style: str | None = None
    key: str | None = None  # the id of the API key that asked for it
    status: JobStatus = "queued"
    progress: float = 0.0
    created: float = field(default_factory=time.time)
    started: float | None = None
    finished: float | None = None
    result: ProviderResult | None = None
    error: Exception | None = None  # usually an ApiError; kept as raised for the legacy endpoint
    done: asyncio.Event = field(default_factory=asyncio.Event)
    task: asyncio.Task | None = None
    data: list[Any] | None = None  # the formatted images, built once on first read
    params: dict = field(default_factory=dict)  # size, steps, cfg and the user's negative: for history and per-step time
    n: int = 1  # images asked for, so a key's daily cap can count the ones still waiting

    @property
    def elapsed_ms(self) -> int | None:
        if self.started is None:
            return None
        return int(((self.finished or time.time()) - self.started) * 1000)

    @property
    def api_error(self) -> ApiError | None:
        if self.error is None or isinstance(self.error, ApiError):
            return self.error
        return ApiError("workflow_failed", str(self.error) or type(self.error).__name__)


class JobQueue:
    def __init__(
        self,
        max_concurrent: int,
        queue_timeout_seconds: float,
        run_timeout_seconds: float,
        keep_seconds: float,
        on_finish: Callable[[Job], None] | None = None,  # the job log
    ) -> None:
        self.on_finish = on_finish
        self._slots = asyncio.Semaphore(max_concurrent)
        self.queue_timeout = queue_timeout_seconds
        self.run_timeout = run_timeout_seconds
        self.keep_seconds = keep_seconds
        self.jobs: dict[str, Job] = {}
        self._listeners: set[asyncio.Queue[dict]] = set()

    def submit(
        self, run: Runner, *, model: str, prompt: str, response_format: str, local: bool, style: str | None = None,
        key: str | None = None, params: dict | None = None, n: int = 1,
    ) -> Job:
        self._prune()
        job = Job(uuid.uuid4().hex, model, prompt, response_format, local, style, key, params=params or {}, n=n)
        self.jobs[job.id] = job
        self._emit("queued", job)
        job.task = asyncio.create_task(self._drive(job, run))
        return job

    def get(self, job_id: str) -> Job:
        job = self.jobs.get(job_id)
        if job is None:
            raise ApiError("not_found", f"No job '{job_id}'. Finished jobs are kept for {int(self.keep_seconds)}s.")
        return job

    def cancel(self, job: Job) -> None:
        """Drops a queued job; stops a running one (ComfyUI is interrupted). Finished jobs stay."""
        if job.task is not None and not job.done.is_set():
            job.task.cancel()

    async def wait(self, job: Job) -> ProviderResult:
        """For the synchronous endpoints: the result, or the job's error raised as it was."""
        try:
            await job.done.wait()
        except asyncio.CancelledError:
            self.cancel(job)  # the caller went away; don't keep the GPU busy for nobody
            raise
        if job.error is not None:
            raise job.error
        if job.result is None:
            raise ApiError("workflow_failed", "The job was cancelled.")
        return job.result

    def queue_position(self, job: Job) -> int:
        """1 = next to run; 0 once it runs (cloud jobs never wait)."""
        if job.status != "queued" or not job.local:
            return 0
        ahead = [j for j in self.jobs.values() if j.local and j.status == "queued" and j.created < job.created]
        return len(ahead) + 1

    def view(self, job: Job) -> dict:
        err = job.api_error
        return {
            "id": job.id,
            "status": job.status,
            "model": job.model,
            "queue_position": self.queue_position(job),
            "progress": round(job.progress, 3),
            "created": int(job.created),
            "elapsed_ms": job.elapsed_ms,
            "steps": job.params.get("steps"),
            "error": {"code": err.code, "message": err.message} if err else None,
        }

    def subscribe(self) -> asyncio.Queue[dict]:
        # shortcut: a listener more than 256 events behind misses events; resync with GET /jobs/{id}.
        listener: asyncio.Queue[dict] = asyncio.Queue(maxsize=256)
        self._listeners.add(listener)
        return listener

    def unsubscribe(self, listener: asyncio.Queue[dict]) -> None:
        self._listeners.discard(listener)

    async def shutdown(self) -> None:
        tasks = [j.task for j in self.jobs.values() if j.task is not None and not j.task.done()]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def _drive(self, job: Job, run: Runner) -> None:
        try:
            if job.local:
                try:
                    await asyncio.wait_for(self._slots.acquire(), self.queue_timeout)
                except TimeoutError:
                    raise ApiError(
                        "timeout", f"Waited over {self.queue_timeout:.0f}s in the queue; the engine is busy."
                    ) from None
            try:
                job.status, job.started = "running", time.time()
                self._emit("started", job)
                try:
                    job.result = await asyncio.wait_for(run(lambda v: self._progress(job, v), lambda b: self._preview(job, b)), self.run_timeout)
                except TimeoutError:
                    raise ApiError("timeout", f"Generation took over {self.run_timeout:.0f}s.") from None
            finally:
                if job.local:
                    self._slots.release()
            job.status, job.progress = "done", 1.0
        except asyncio.CancelledError:
            job.status = "cancelled"
        except Exception as exc:  # noqa: BLE001 - every failure is reported on the job
            if not isinstance(exc, ApiError):
                logger.exception("Job %s failed", job.id)
            job.status, job.error = "failed", exc
        finally:
            job.finished = time.time()
            job.done.set()
            self._emit(job.status, job)
            if self.on_finish is not None:
                self.on_finish(job)

    def _progress(self, job: Job, value: float) -> None:
        job.progress = value
        self._emit("progress", job)

    def _preview(self, job: Job, image: bytes) -> None:
        # Only on the event, never kept on the job: previews are for whoever is watching right now.
        self._emit("preview", job, preview=base64.b64encode(image).decode())

    def _emit(self, event: str, job: Job, **extra: Any) -> None:
        message = {"event": event, **self.view(job), **extra}
        for listener in self._listeners:
            with contextlib.suppress(asyncio.QueueFull):
                listener.put_nowait(message)

    def _prune(self) -> None:
        # shortcut: jobs (and their images) live in memory and are lost on restart; fine for one
        # desktop user, persist them if the host ever serves many clients.
        cutoff = time.time() - self.keep_seconds
        for job_id in [j.id for j in self.jobs.values() if j.finished is not None and j.finished < cutoff]:
            del self.jobs[job_id]
