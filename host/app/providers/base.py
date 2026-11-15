"""The contract every provider implements, local or cloud."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from app.catalog import CatalogModel, ResolvedParams
from app.comfy_client import ComfyImageOutput, ProgressFn
from app.styles import ComposedPrompt


@dataclass(frozen=True)
class ProviderRequest:
    model: CatalogModel
    prompt: ComposedPrompt
    params: ResolvedParams  # already checked against the model's limits
    seed: int | None = None  # None or -1: random


@dataclass(frozen=True)
class ProviderResult:
    images: list[ComfyImageOutput]
    seed: int | None
    job_ref: str  # the provider's own id for the run, e.g. the ComfyUI prompt_id


class Provider(Protocol):
    """Failures are raised as ApiError with one of the codes in app.errors. Cancelling the
    generate() task cancels the run; a provider that can, stops the work on its side too."""

    id: str

    async def health(self) -> None: ...

    async def generate(self, request: ProviderRequest, on_progress: ProgressFn | None = None) -> ProviderResult: ...
