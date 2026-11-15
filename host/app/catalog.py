"""Catalog models: loading, and checking request params against each model's limits."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from app.errors import ApiError

if TYPE_CHECKING:
    from app.styles import Style

ID_PATTERN = r"^[a-z0-9][a-z0-9_-]*$"
Quality = Literal["fast", "standard", "best"]


class _Strict(BaseModel):
    # Catalog files are hand-edited; a typo in a key should fail loudly, not be ignored.
    model_config = ConfigDict(extra="forbid")


class License(_Strict):
    name: str
    url: str
    commercial_ok: bool


class Params(_Strict):
    steps: int | None = None
    cfg: float | None = None


class Defaults(Params):
    shape: str


class Limits(_Strict):
    sizes: dict[str, tuple[int, int]] = Field(min_length=1)  # named shapes, e.g. "square"
    custom_size: bool = False  # False: only the named sizes are allowed
    multiple_of: int = Field(default=8, ge=1)
    min_side: int = Field(default=256, ge=1)
    max_pixels: int | None = None
    steps: tuple[int, int] | None = None  # None: the model takes no steps value
    cfg: tuple[float, float] | None = None
    n_max: int = Field(default=1, ge=1)
    supports_negative: bool = True
    supports_seed: bool = True


class CatalogModel(_Strict):
    id: str = Field(pattern=ID_PATTERN)
    name: str
    family: str
    provider: str = "comfyui"
    license: License
    quality_rank: int = 0
    workflow: str | None = None
    negative: str = ""
    requires_hf_token: bool = False
    defaults: Defaults
    quality: dict[Quality, Params] = Field(default_factory=dict)
    limits: Limits

    @model_validator(mode="after")
    def _check_consistency(self) -> CatalogModel:
        if self.provider == "comfyui" and not self.workflow:
            raise ValueError("comfyui models need a 'workflow'.")
        if self.defaults.shape not in self.limits.sizes:
            raise ValueError(f"defaults.shape '{self.defaults.shape}' is not in limits.sizes.")
        for name, (width, height) in self.limits.sizes.items():
            _check_size(self.limits, width, height, label=f"limits.sizes.{name}", named_ok=True)
        for label, params in [("defaults", self.defaults), *self.quality.items()]:
            try:
                _check_steps_cfg(self.limits, params.steps, params.cfg)
            except ApiError as exc:
                raise ValueError(f"{label}: {exc.message}") from exc
        return self


class CatalogError(ValueError):
    """A shipped or user catalog file is invalid. Raised at load time, not per request."""


@dataclass(frozen=True)
class ResolvedParams:
    width: int
    height: int
    steps: int | None
    cfg: float | None
    n: int


def load_catalog(catalog_dir: Path) -> dict[str, CatalogModel]:
    """Load every `models/*.json`; the file name must match the model id."""
    models: dict[str, CatalogModel] = {}
    for path in sorted((catalog_dir / "models").glob("*.json")):
        try:
            model = CatalogModel.model_validate(json.loads(path.read_text(encoding="utf-8")))
        except (json.JSONDecodeError, ValidationError) as exc:
            raise CatalogError(f"{path.name}: {exc}") from exc
        if model.id != path.stem:
            raise CatalogError(f"{path.name}: id '{model.id}' does not match the file name.")
        if model.workflow and not (catalog_dir / "workflows" / f"{model.workflow}.json").is_file():
            raise CatalogError(f"{path.name}: workflow '{model.workflow}' not found.")
        models[model.id] = model
    return models


def resolve_params(
    model: CatalogModel,
    *,
    style: Style | None = None,
    quality: Quality | None = None,
    shape: str | None = None,
    width: int | None = None,
    height: int | None = None,
    steps: int | None = None,
    cfg: float | None = None,
    n: int = 1,
) -> ResolvedParams:
    """Pick each value by precedence (request > style > quality preset > model default), then check it."""
    overrides = style.overrides if style and (not style.models or model.id in style.models) else None
    preset = model.quality.get(quality) if quality else None

    def pick(field: str, requested):
        if requested is not None:
            return requested
        for source in (overrides, preset, model.defaults):
            value = getattr(source, field, None) if source else None
            if value is not None:
                return value
        return None

    if (width is None) != (height is None):
        raise ApiError("bad_params", "Send both width and height, or neither.")
    if width is None:
        # A style's shape only applies when this model has a size by that name.
        if shape is None and overrides and overrides.shape in model.limits.sizes:
            shape = overrides.shape
        shape = shape or model.defaults.shape
        if shape not in model.limits.sizes:
            raise ApiError(
                "bad_params",
                f"Unknown shape '{shape}' for {model.name}. Use one of: {', '.join(model.limits.sizes)}.",
            )
        width, height = model.limits.sizes[shape]

    resolved = ResolvedParams(width, height, pick("steps", steps), pick("cfg", cfg), n)
    check_params(model, resolved)
    return resolved


def check_params(model: CatalogModel, params: ResolvedParams) -> None:
    """Reject anything outside the model's limits before it reaches the GPU or a paid API."""
    limits = model.limits
    _check_size(limits, params.width, params.height, label="size", named_ok=False)
    _check_steps_cfg(limits, params.steps, params.cfg)
    if not 1 <= params.n <= limits.n_max:
        raise ApiError("bad_params", f"n must be between 1 and {limits.n_max} for {model.name}.")


def _check_size(limits: Limits, width: int, height: int, *, label: str, named_ok: bool) -> None:
    if not named_ok and (width, height) in limits.sizes.values():
        return
    if not named_ok and not limits.custom_size:
        allowed = ", ".join(f"{w}x{h}" for w, h in limits.sizes.values())
        raise ApiError("bad_params", f"{label} {width}x{height} is not allowed. Use one of: {allowed}.")
    if width % limits.multiple_of or height % limits.multiple_of:
        raise ApiError("bad_params", f"{label} {width}x{height}: both sides must be multiples of {limits.multiple_of}.")
    if min(width, height) < limits.min_side:
        raise ApiError("bad_params", f"{label} {width}x{height}: each side must be at least {limits.min_side}.")
    if limits.max_pixels and width * height > limits.max_pixels:
        raise ApiError("bad_params", f"{label} {width}x{height} is over {limits.max_pixels} pixels.")


def _check_steps_cfg(limits: Limits, steps: int | None, cfg: float | None) -> None:
    for name, value, bounds in (("steps", steps, limits.steps), ("cfg", cfg, limits.cfg)):
        if value is None:
            continue
        if bounds is None:
            raise ApiError("bad_params", f"This model does not take a {name} value.")
        if not bounds[0] <= value <= bounds[1]:
            raise ApiError("bad_params", f"{name} must be between {bounds[0]} and {bounds[1]}.")
