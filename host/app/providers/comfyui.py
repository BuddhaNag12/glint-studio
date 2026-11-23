"""Runs catalog models (and the user's own workflows) on a ComfyUI server."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from app.comfy_client import (
    ComfyClient,
    ComfyConnectionError,
    ComfyExecutionError,
    ComfyTimeoutError,
    PreviewFn,
    ProgressFn,
)
from app.errors import ApiError
from app.local_models import variant_installed
from app.providers.base import ProviderRequest, ProviderResult
from app.schemas import GenerateRequest
from app.workflow import (
    WorkflowMapping,
    WorkflowNotFoundError,
    WorkflowValidationError,
    load_workflow_bundle,
    prepare_workflow,
    use_model_files,
)


class ComfyUIProvider:
    id = "comfyui"

    def __init__(
        self,
        client: ComfyClient,
        workflows_dir: Path,
        timeout_seconds: float,
        roots: Callable[[], list[Path]] = lambda: [],
        extra_workflows_dirs: list[Path] = (),
    ) -> None:
        self.client = client
        self.workflows_dir = workflows_dir  # the catalog's workflow templates
        self.extra_workflows_dirs = [d for d in extra_workflows_dirs if d]  # online catalog, ComfyUI's templates
        self.timeout_seconds = timeout_seconds
        self.roots = roots  # model folders, to find which variant is downloaded

    async def health(self) -> None:
        await self.system_stats()

    async def system_stats(self) -> dict:
        """ComfyUI's /system_stats: its devices with total and free VRAM."""
        try:
            return await self.client.check_health() or {}
        except ComfyConnectionError as exc:
            raise ApiError("engine_down", str(exc)) from exc

    async def generate(
        self, request: ProviderRequest, on_progress: ProgressFn | None = None, on_preview: PreviewFn | None = None
    ) -> ProviderResult:
        p = request.params
        # model_construct: the composed prompt may pass GenerateRequest's 4000-char cap, and the
        # values were already checked against the model's limits.
        fields = GenerateRequest.model_construct(
            prompt=request.prompt.positive,
            negative_prompt=request.prompt.negative,
            width=p.width,
            height=p.height,
            steps=p.steps,
            cfg=p.cfg,
            seed=request.seed,
        )
        try:
            name = request.model.workflow or ""
            # A built-in template wins over an online one of the same name.
            here = next((d for d in [self.workflows_dir, *self.extra_workflows_dirs] if (d / f"{name}.json").is_file()), self.workflows_dir)
            workflow, mapping = load_workflow_bundle(here, name)
            roots = self.roots()
            # The best installed variant; none installed keeps the template's names (ComfyUI then reports model_missing).
            variant = next((v for v in request.model.variants if variant_installed(roots, v)), None)
            if variant:
                use_model_files(workflow, [f.dest for f in variant.files])
            return await self.run_workflow(workflow, mapping, fields, on_progress, on_preview)
        except (WorkflowNotFoundError, WorkflowValidationError) as exc:
            raise ApiError("workflow_failed", f"{request.model.name}: {exc}") from exc

    async def run_workflow(
        self,
        workflow: dict[str, Any],
        mapping: WorkflowMapping,
        fields: GenerateRequest,
        on_progress: ProgressFn | None = None,
        on_preview: PreviewFn | None = None,
    ) -> ProviderResult:
        """Fill and run any API-format workflow. Raises WorkflowValidationError when the mapping
        doesn't fit the workflow, so callers running a user's own workflow can report it as such."""
        payload, seed = prepare_workflow(workflow, mapping, fields)
        try:
            prompt_id, images = await self.client.generate_images(
                payload,
                output_node_id=mapping.output_node_id,
                timeout_seconds=self.timeout_seconds,
                on_progress=on_progress,
                on_preview=on_preview,
            )
        except ComfyTimeoutError as exc:
            raise ApiError("timeout", str(exc)) from exc
        except ComfyConnectionError as exc:
            raise ApiError("engine_down", str(exc)) from exc
        except ComfyExecutionError as exc:
            # A loader node pointing at a model file ComfyUI doesn't have fails as "value_not_in_list".
            code = "model_missing" if "value_not_in_list" in str(exc) else "workflow_failed"
            raise ApiError(code, str(exc)) from exc
        return ProviderResult(images=images, seed=seed, job_ref=prompt_id)
