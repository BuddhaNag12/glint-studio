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
