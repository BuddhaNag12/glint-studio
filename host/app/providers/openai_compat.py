"""Any OpenAI-compatible image API (OpenAI itself and the services that copy its API),
configured in providers.json with the key read from an environment variable."""

from __future__ import annotations

import base64
import binascii
import json
import logging
import os
import time
from pathlib import Path
from typing import Literal

import httpx
from dotenv import dotenv_values
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.catalog import ID_PATTERN, CatalogModel
from app.comfy_client import ComfyImageOutput, ProgressFn
from app.errors import ApiError
from app.providers.base import ProviderRequest, ProviderResult

logger = logging.getLogger(__name__)

IMAGE_TYPES = {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp"}


class RemoteProviderConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=ID_PATTERN)
    type: Literal["openai_compat"]
    name: str = ""
    base_url: str = Field(pattern=r"^https?://")  # e.g. https://api.openai.com/v1
    api_key_env: str | None = None  # env var holding the key; None for servers that need none
    cloud: bool = True  # False for a server on your own machine or network
    models: list[dict] = Field(min_length=1)  # catalog model entries; provider/cloud are filled in


def load_remote_providers(
    path: Path | None,
    taken_ids: set[str],
    http_client: httpx.AsyncClient,
) -> tuple[dict[str, OpenAICompatProvider], dict[str, CatalogModel]]:
    """Read providers.json. It is hand-edited user config, so a bad provider is logged and
    skipped rather than stopping the host."""
    if path is None or not path.is_file():
        return {}, {}
    try:
        entries = json.loads(path.read_text(encoding="utf-8")).get("providers", [])
    except (json.JSONDecodeError, AttributeError) as exc:
        logger.warning("Ignoring %s: %s", path.name, exc)
        return {}, {}

    providers: dict[str, OpenAICompatProvider] = {}
    models: dict[str, CatalogModel] = {}
    for entry in entries:
        try:
            cfg = RemoteProviderConfig.model_validate(entry)
            if cfg.id == "comfyui" or cfg.id in providers:
                raise ValueError(f"provider id '{cfg.id}' is already used.")
            found = [
                CatalogModel.model_validate({**m, "provider": cfg.id, "cloud": cfg.cloud})
                for m in cfg.models
            ]
            clash = {m.id for m in found} & (taken_ids | set(models))
            if clash or len({m.id for m in found}) != len(found):
                raise ValueError(f"model ids already used: {', '.join(sorted(clash)) or 'duplicates'}.")
        except (ValidationError, ValueError) as exc:
            name = entry.get("id", "?") if isinstance(entry, dict) else "?"
            logger.warning("Skipping provider '%s' in %s: %s", name, path.name, exc)
            continue
        providers[cfg.id] = OpenAICompatProvider(cfg, http_client)
        models.update({m.id: m for m in found})
    return providers, models


def _read_key(env_name: str) -> str | None:
    # Settings only maps its own fields from .env, so look there too for the provider's key.
    value = os.environ.get(env_name) or dotenv_values(".env").get(env_name)
    return value.strip() if value and value.strip() else None


def _image(content: bytes, content_type: str | None = None) -> ComfyImageOutput:
    mime = (content_type or "").split(";")[0].strip()
    if mime not in IMAGE_TYPES:
        if content.startswith(b"\x89PNG"):
            mime = "image/png"
        elif content.startswith(b"\xff\xd8"):
            mime = "image/jpeg"
        elif content[:4] == b"RIFF" and content[8:12] == b"WEBP":
            mime = "image/webp"
        else:
            raise ApiError("provider_rejected", "The provider returned something that isn't an image.")
    return ComfyImageOutput(
        filename=f"remote{IMAGE_TYPES[mime]}",
        content=content,
        mime_type=mime,
        extension=IMAGE_TYPES[mime],
    )


class OpenAICompatProvider:
    def __init__(self, config: RemoteProviderConfig, http_client: httpx.AsyncClient) -> None:
        self.id = config.id
        self.config = config
        self.http = http_client
        # Read once and kept in memory only; never logged or returned by the API.
        self._key = _read_key(config.api_key_env) if config.api_key_env else None
        if config.api_key_env and not self._key:
            logger.warning("Provider '%s': %s is not set.", config.id, config.api_key_env)

    async def health(self) -> None:
        # No probe: listing or generating would spend the user's quota.
        return None

    async def check(self) -> None:
        """Tests the key with a call that costs nothing: listing the provider's models."""
        name = self.config.name or self.id
        if self.config.api_key_env and not self._key:
            raise ApiError("provider_auth", f"No key is set for {name}.")
        headers = {"Authorization": f"Bearer {self._key}"} if self._key else {}
        try:
            res = await self.http.get(f"{self.config.base_url.rstrip('/')}/models", headers=headers)
        except httpx.TimeoutException as exc:
            raise ApiError("timeout", f"{name} took too long.") from exc
        except httpx.HTTPError as exc:
            raise ApiError("provider_down", f"Can't reach {name}: {exc}") from exc
        _raise_for_status(res, name)

    async def generate(self, request: ProviderRequest, on_progress: ProgressFn | None = None) -> ProviderResult:
        # These APIs report no progress; cancelling the task drops the HTTP request.
        model, p = request.model, request.params
        if self.config.api_key_env and not self._key:
            raise ApiError("provider_auth", f"Set {self.config.api_key_env} to use {model.name}.")

        # Only fields the model declares support for: strict APIs reject unknown parameters.
        # response_format is left out because some models (gpt-image-*) reject it; both
        # b64_json and url answers are handled below.
        body: dict = {
            "model": model.remote_id or model.id,
            "prompt": request.prompt.positive,
            "n": p.n,
            "size": f"{p.width}x{p.height}",
        }
        if request.prompt.negative:
            body["negative_prompt"] = request.prompt.negative
        if p.steps is not None:
            body["steps"] = p.steps
        if model.limits.supports_seed and request.seed is not None and request.seed >= 0:
            body["seed"] = request.seed
        headers = {"Authorization": f"Bearer {self._key}"} if self._key else {}

        url = f"{self.config.base_url.rstrip('/')}/images/generations"
        try:
            res = await self.http.post(url, json=body, headers=headers)
            _raise_for_status(res, self.config.name or self.id)
            data = res.json().get("data") or []
            images = [await self._fetch(item) for item in data]
        except httpx.TimeoutException as exc:
            raise ApiError("timeout", f"{self.config.name or self.id} took too long.") from exc
        except httpx.HTTPError as exc:
            raise ApiError("provider_down", f"Can't reach {self.config.name or self.id}: {exc}") from exc
        except (ValueError, AttributeError, TypeError) as exc:
            raise ApiError("provider_down", f"Unexpected answer from {self.config.name or self.id}.") from exc
        if not images:
            raise ApiError("provider_rejected", "The provider returned no images.")
        seed = body.get("seed")
        return ProviderResult(images=images, seed=seed, job_ref=f"{self.id}-{int(time.time() * 1000)}")

    async def _fetch(self, item: dict) -> ComfyImageOutput:
        if item.get("b64_json"):
            try:
                return _image(base64.b64decode(item["b64_json"], validate=True))
            except binascii.Error as exc:
                raise ValueError("bad base64") from exc
        if item.get("url"):
            res = await self.http.get(item["url"])
            res.raise_for_status()
            return _image(res.content, res.headers.get("content-type"))
        raise ValueError("image has neither b64_json nor url")


def _raise_for_status(res: httpx.Response, name: str) -> None:
    if res.status_code < 400:
        return
    try:
        detail = res.json()["error"]["message"]
    except (ValueError, KeyError, TypeError):
        detail = res.text[:300] or res.reason_phrase
    message = f"{name}: {detail}"
    if res.status_code in (401, 403):
        raise ApiError("provider_auth", message)
    if res.status_code == 429:
        raise ApiError("provider_quota", message)
    if res.status_code == 404:
        raise ApiError("model_missing", message)
    if res.status_code < 500:
        raise ApiError("provider_rejected", message)
    raise ApiError("provider_down", message)
