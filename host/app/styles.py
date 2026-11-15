"""Style presets: loading built-in and user styles, and composing the final prompt."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.catalog import ID_PATTERN, CatalogError, CatalogModel
from app.errors import ApiError

logger = logging.getLogger(__name__)


class StyleOverrides(BaseModel):
    model_config = ConfigDict(extra="forbid")

    steps: int | None = None
    cfg: float | None = None
    shape: str | None = None  # used only when the model has a size by this name


class Style(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=ID_PATTERN)
    name: str = Field(min_length=1, max_length=64)
    icon: str = ""
    prefix: str = Field(default="", max_length=4000)
    suffix: str = Field(default="", max_length=4000)
    negative: str = Field(default="", max_length=4000)
    overrides: StyleOverrides = Field(default_factory=StyleOverrides)
    models: list[str] | None = None  # None: works with every model


@dataclass(frozen=True)
class ComposedPrompt:
    positive: str
    negative: str | None  # None when empty or when the model ignores negatives
    negative_dropped: bool  # True when negative text existed but the model can't use it


def _load_style(path: Path) -> Style:
    style = Style.model_validate(json.loads(path.read_text(encoding="utf-8")))
    if style.id != path.stem:
        raise ValueError(f"id '{style.id}' does not match the file name.")
    return style


def load_styles(builtin_dir: Path, user_dir: Path | None = None) -> dict[str, Style]:
    """Built-in styles, with user copies (same id) replacing them. A bad built-in file is a bug and
    raises; a bad user file is logged and skipped so one hand edit can't stop the host."""
    styles: dict[str, Style] = {}
    for path in sorted(builtin_dir.glob("*.json")):
        try:
            styles[path.stem] = _load_style(path)
        except (json.JSONDecodeError, ValidationError, ValueError) as exc:
            raise CatalogError(f"{path.name}: {exc}") from exc
    if user_dir and user_dir.is_dir():
        for path in sorted(user_dir.glob("*.json")):
            try:
                styles[path.stem] = _load_style(path)
            except (json.JSONDecodeError, ValidationError, ValueError) as exc:
                logger.warning("Skipping user style %s: %s", path.name, exc)
    return styles


def compose(
    model: CatalogModel,
    prompt: str,
    *,
    style: Style | None = None,
    negative: str | None = None,
) -> ComposedPrompt:
    """positive = prefix. prompt. suffix; negative = style + model + user (additive, never replaced)."""
    if style and style.models is not None and model.id not in style.models:
        raise ApiError("bad_params", f"Style '{style.name}' does not support {model.name}.")

    parts = [p.strip() for p in (style.prefix if style else "", prompt, style.suffix if style else "")]
    parts = [p for p in parts if p]
    # Drop a trailing period on all but the last part so the ". " joiner doesn't double it.
    positive = ". ".join([p.rstrip(".").rstrip() for p in parts[:-1]] + parts[-1:])

    negatives = [n.strip() for n in (style.negative if style else "", model.negative, negative or "")]
    joined = ", ".join(n for n in negatives if n)
    if not model.limits.supports_negative:
        return ComposedPrompt(positive, None, negative_dropped=bool(joined))
    return ComposedPrompt(positive, joined or None, negative_dropped=False)
