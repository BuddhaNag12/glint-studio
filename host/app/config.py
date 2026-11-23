"""Application configuration loaded from environment variables or .env."""

from __future__ import annotations

import hashlib
import hmac
import json
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import urlsplit, urlunsplit

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

PLACEHOLDER_API_KEY = "replace-with-a-strong-random-token"


class Settings(BaseSettings):
    """Runtime settings for the ComfyUI API bridge."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    comfyui_url: str = "http://127.0.0.1:8188"
    # The desktop app sets this false until setup installs the engine (PyTorch and ComfyUI).
    engine_installed: bool = True
    api_key: str = ""
    url_signing_secret: str = ""
    cors_origins: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["http://localhost:3000", "http://localhost:5173"]
    )
    public_base_url: str = "http://127.0.0.1:8000"
    # The first image after a start also loads the model (about 19 GB for Z-Image Turbo); on Apple Silicon that alone runs past 2 minutes.
    request_timeout_seconds: float = Field(default=600.0, gt=0)
    max_concurrent_jobs: int = Field(default=1, ge=1)
    queue_timeout_seconds: float = Field(default=600.0, gt=0)  # longest wait for a free slot
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
    # Set by the desktop app; unset when the host runs on its own.
    models_dir: Path | None = None  # where model downloads go (ComfyUI reads it)
    hardware_file: Path | None = None  # the app's machine scan, for model recommendations
    stats_db: Path | None = None  # SQLite job log for the dashboard; unset keeps it in memory
    dashboard_dir: Path | None = None  # the built dashboard web page
    network_file: Path | None = None  # sharing the dashboard asks for, {"lan", "tunnel"}; the app applies it
    tunnel_file: Path | None = None  # the app's report on the public link, {"status", "url", "error"}
    logs_dir: Path | None = None  # the app's process logs: host.log, comfyui.log, tunnel.log
    # engine.json, {"preload", "folders"}: the app links the folders into ComfyUI (extra_model_paths.yaml
    # beside it) and restarts it when they change. Unset: no linked folders, no preload.
    engine_file: Path | None = None
    catalog_url: str = ""  # catalog.json with models added since this release; empty: don't check
    keys_file: Path | None = None  # keys.json: extra API keys, hashed; unset keeps them in memory
    serve_on_network: bool = False  # the app started this host on every address, not just 127.0.0.1
    # Host header values the dashboard pages accept; ["*"] accepts any. Guards them against DNS
    # rebinding. The API needs the key in a header, so it answers any Host (LAN, tunnel).
    allowed_hosts: Annotated[list[str], NoDecode] = Field(default_factory=lambda: ["*"])

    @field_validator(
        "comfyui_dir", "comfyui_temp_dir", "comfyui_output_dir", "user_styles_dir",
        "providers_file", "models_dir", "hardware_file", "stats_db", "dashboard_dir", "network_file", "tunnel_file",
        "logs_dir", "engine_file",
        mode="before",
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

    @field_validator("cors_origins", "allowed_hosts", mode="before")
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
                    raise ValueError("JSON value must be a list.")
                return [str(item).strip() for item in parsed if str(item).strip()]
            return [item.strip() for item in stripped.split(",") if item.strip()]
        raise ValueError("Must be a list or comma-separated string.")

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
