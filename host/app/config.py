"""Application configuration loaded from environment variables or .env."""

from __future__ import annotations

import hashlib
import hmac
import json
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

PLACEHOLDER_API_KEY = "replace-with-a-strong-random-token"


class Settings(BaseSettings):
    """Runtime settings for the ComfyUI API bridge."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    comfyui_url: str = "http://127.0.0.1:8188"
    api_key: str = ""
    url_signing_secret: str = ""
    cors_origins: list[str] = Field(
        default_factory=lambda: ["http://localhost:3000", "http://localhost:5173"]
    )
    public_base_url: str = "http://127.0.0.1:8000"
    request_timeout_seconds: float = Field(default=120.0, gt=0)
    max_concurrent_jobs: int = Field(default=1, ge=1)
    output_dir: Path = Path("generated_images")
    workflows_dir: Path = Path("workflows")  # the user's own ComfyUI workflows
    catalog_dir: Path = Path(__file__).resolve().parents[2] / "catalog"
    user_styles_dir: Path | None = None  # styles here replace built-ins with the same id
    providers_file: Path | None = None  # providers.json: remote image APIs and their models
    sign_image_urls: bool = True
    image_url_ttl_seconds: int = Field(default=3600, ge=1)
    clear_comfy_history: bool = True
    cleanup_comfy_files: bool = True
    comfyui_dir: Path | None = None
    comfyui_temp_dir: Path | None = None
    comfyui_output_dir: Path | None = None

    @field_validator(
        "comfyui_dir", "comfyui_temp_dir", "comfyui_output_dir", "user_styles_dir",
        "providers_file", mode="before"
    )
    @classmethod
    def parse_optional_path(cls, value: Any) -> Any:
        """Treat empty path strings in .env as unset and expand ~ home prefixes."""
        if value is None:
            return None
        if isinstance(value, Path):
            return value.expanduser()
        if isinstance(value, str):
            stripped = value.strip()
            if not stripped:
                return None
            return Path(stripped).expanduser()
        return value

    @field_validator("cors_origins", mode="before")
    @classmethod
    def parse_cors_origins(cls, value: Any) -> list[str]:
        """Accept either a JSON array, comma-separated string, or list of origins."""
        if isinstance(value, list):
            return [str(item).strip() for item in value if str(item).strip()]
        if isinstance(value, str):
            stripped = value.strip()
            if not stripped:
                return []
            if stripped.startswith("["):
                parsed = json.loads(stripped)
                if not isinstance(parsed, list):
                    raise ValueError("CORS_ORIGINS JSON value must be a list.")
                return [str(item).strip() for item in parsed if str(item).strip()]
            return [item.strip() for item in stripped.split(",") if item.strip()]
        raise ValueError("CORS_ORIGINS must be a list or comma-separated string.")

    @field_validator("comfyui_url", "public_base_url")
    @classmethod
    def strip_trailing_slash(cls, value: str) -> str:
        """Normalize URLs without trailing slashes."""
        return value.strip().rstrip("/")

    @property
    def is_api_key_configured(self) -> bool:
        """Return True when a non-placeholder API key is configured."""
        key = self.api_key.strip()
        return bool(key) and key != PLACEHOLDER_API_KEY

    @property
    def comfyui_http_url(self) -> str:
        """Normalized HTTP base URL for ComfyUI."""
        return self.comfyui_url.rstrip("/")

    @property
    def comfyui_ws_url(self) -> str:
        """WebSocket base URL derived from COMFYUI_URL."""
        parts = urlsplit(self.comfyui_http_url)
        ws_scheme = "wss" if parts.scheme == "https" else "ws"
        return urlunsplit((ws_scheme, parts.netloc, parts.path.rstrip("/"), "", ""))

    @property
    def effective_signing_secret(self) -> bytes:
        """Return the HMAC secret used to sign temporary image URLs."""
        if self.url_signing_secret.strip():
            return self.url_signing_secret.strip().encode("utf-8")
        if self.is_api_key_configured:
            return hmac.new(
                self.api_key.strip().encode("utf-8"),
                b"comfy-api-bridge:image-signing",
                hashlib.sha256,
            ).digest()
        raise ValueError(
            "URL_SIGNING_SECRET or API_KEY must be set when SIGN_IMAGE_URLS is enabled."
        )
