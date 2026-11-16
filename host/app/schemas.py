"""Pydantic request and response schemas for the API endpoints."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator


class GenerateRequest(BaseModel):
    """Payload accepted by POST /api/v1/generate."""

    prompt: str = Field(..., min_length=1, max_length=4000)
    negative_prompt: str | None = Field(default=None, max_length=4000)
    width: int | None = Field(default=None, ge=64, le=4096)
    height: int | None = Field(default=None, ge=64, le=4096)
    steps: int | None = Field(default=None, ge=1, le=150)
    cfg: float | None = Field(default=None, ge=0.0, le=30.0)
    seed: int | None = Field(default=None, ge=-1, le=2**63 - 1)
    workflow: str = Field(default="default", min_length=1, max_length=64)
    response_format: Literal["image", "url", "base64", "both"] = "image"

    @field_validator("prompt")
    @classmethod
    def validate_non_blank_prompt(cls, value: str) -> str:
        """Reject whitespace-only prompts."""
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("prompt must not be blank.")
        return cleaned


class GeneratedImage(BaseModel):
    """Metadata and encoded representations for a single generated image."""

    filename: str
    mime_type: str
    url: str | None = None
    base64: str | None = None
    data_url: str | None = None


class GenerateResponse(BaseModel):
    """Synchronous JSON response returned to the client."""

    id: str
    prompt_id: str
    status: Literal["completed"] = "completed"
    workflow: str
    seed: int | None = None
    images: list[GeneratedImage]
    image_url: str | None = None
    image_base64: str | None = None
    data_url: str | None = None
    elapsed_ms: int


class HealthResponse(BaseModel):
    """Service liveness and ComfyUI connectivity status."""

    status: Literal["ok", "degraded"]
    comfyui_connected: bool
    comfyui_url: str
    default_workflow_present: bool
    detail: str | None = None


class ImagesRequest(BaseModel):
    """POST /v1/images/generations: OpenAI's fields, plus style, negative_prompt and seed.
    Unknown OpenAI fields (user, background, output_format, ...) are ignored."""

    prompt: str = Field(..., min_length=1, max_length=4000)
    model: str | None = None  # None: the default catalog model
    n: int = Field(default=1, ge=1, le=10)
    size: str | None = Field(default=None, pattern=r"^(auto|\d{2,5}x\d{2,5})$")
    response_format: Literal["b64_json", "url"] = "b64_json"
    quality: Literal["fast", "standard", "best", "auto"] | None = None
    style: str | None = None  # a style id from GET /api/v1/styles
    negative_prompt: str | None = Field(default=None, max_length=4000)
    seed: int | None = Field(default=None, ge=-1, le=2**63 - 1)
    steps: int | None = Field(default=None, ge=1, le=150)  # extension: checked against the model's limits
    cfg: float | None = Field(default=None, ge=0.0, le=30.0)

    @field_validator("prompt")
    @classmethod
    def validate_non_blank_prompt(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("prompt must not be blank.")
        return cleaned


class ImageData(BaseModel):
    b64_json: str | None = None
    url: str | None = None


class ImagesResponse(BaseModel):
    created: int
    data: list[ImageData]
    model: str  # extension: the catalog model that ran
    seed: int | None = None  # extension: the seed used, when the model takes one
