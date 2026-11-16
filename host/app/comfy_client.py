"""Asynchronous HTTP and WebSocket client for ComfyUI."""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import httpx
import websockets
from websockets.exceptions import WebSocketException

logger = logging.getLogger(__name__)

# Called with 0..1 as the sampler steps through.
ProgressFn = Callable[[float], None]
# Called with each JPEG/PNG the engine sends while sampling (needs --preview-method on ComfyUI).
PreviewFn = Callable[[bytes], None]


class ComfyConnectionError(RuntimeError):
    """Raised when the bridge cannot connect to ComfyUI."""


class ComfyTimeoutError(TimeoutError):
    """Raised when ComfyUI does not finish within the configured timeout."""


class ComfyExecutionError(RuntimeError):
    """Raised when ComfyUI rejects or fails a workflow execution."""


@dataclass(slots=True)
class ComfyImageOutput:
    """Raw image bytes and metadata fetched from ComfyUI /view."""

    filename: str
    content: bytes
    mime_type: str
    extension: str


MIME_MAP: dict[str, tuple[str, str]] = {
    ".png": ("image/png", ".png"),
    ".webp": ("image/webp", ".webp"),
    ".jpg": ("image/jpeg", ".jpg"),
    ".jpeg": ("image/jpeg", ".jpg"),
}


def infer_mime_and_ext(filename: str) -> tuple[str, str]:
    """Infer MIME type and normalized extension from a filename."""
    suffix = Path(filename).suffix.lower()
    return MIME_MAP.get(suffix, ("image/png", ".png"))


def _infer_image_from_bytes(content: bytes) -> tuple[str, str] | None:
    """Detect supported image MIME type and extension from raw file header bytes."""
    if content.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png", ".png"
    if content.startswith(b"\xff\xd8\xff"):
        return "image/jpeg", ".jpg"
    if len(content) >= 12 and content[:4] == b"RIFF" and content[8:12] == b"WEBP":
        return "image/webp", ".webp"
    return None


def _parse_comfy_dirs_from_argv(argv: Any) -> tuple[Path | None, Path | None, Path | None]:
    """Best-effort extraction of ComfyUI root, temp, and output paths from /system_stats argv."""
    if not isinstance(argv, list):
        return None, None, None

    args = [str(item) for item in argv if isinstance(item, str)]
    explicit_base_dir: Path | None = None
    main_script_dir: Path | None = None
    temp_dir: Path | None = None
    output_dir: Path | None = None

    idx = 0
    while idx < len(args):
        arg = args[idx]
        if arg == "--base-directory" and idx + 1 < len(args):
            explicit_base_dir = Path(args[idx + 1]).expanduser()
            idx += 2
            continue
        if arg.startswith("--base-directory="):
            explicit_base_dir = Path(arg.split("=", 1)[1]).expanduser()
        elif arg == "--temp-directory" and idx + 1 < len(args):
            temp_dir = Path(args[idx + 1]).expanduser()
            idx += 2
            continue
        elif arg.startswith("--temp-directory="):
            temp_dir = Path(arg.split("=", 1)[1]).expanduser()
        elif arg == "--output-directory" and idx + 1 < len(args):
            output_dir = Path(args[idx + 1]).expanduser()
            idx += 2
            continue
        elif arg.startswith("--output-directory="):
            output_dir = Path(arg.split("=", 1)[1]).expanduser()
        elif main_script_dir is None and arg.lower().endswith("main.py"):
            candidate = Path(arg).expanduser()
            if candidate.is_absolute():
                main_script_dir = candidate.parent
        idx += 1

    base_dir = explicit_base_dir or main_script_dir
    if base_dir is not None:
        if temp_dir is not None and not temp_dir.is_absolute():
            temp_dir = base_dir / temp_dir
        if output_dir is not None and not output_dir.is_absolute():
            output_dir = base_dir / output_dir

    return base_dir, temp_dir, output_dir


class ComfyClient:
    """Communicates with ComfyUI's /system_stats, /prompt, /ws, /history, and /view APIs."""

    def __init__(
        self,
        http_url: str,
        ws_url: str,
        http_client: httpx.AsyncClient,
        *,
        clear_history: bool = True,
        cleanup_files: bool = True,
        comfyui_dir: Path | None = None,
        comfyui_temp_dir: Path | None = None,
        comfyui_output_dir: Path | None = None,
    ) -> None:
        self.http_url = http_url.rstrip("/")
        self.ws_url = ws_url.rstrip("/")
        self.http_client = http_client
        self.clear_history = clear_history
        self.cleanup_files = cleanup_files
        self.comfyui_dir = comfyui_dir
        self.comfyui_temp_dir = comfyui_temp_dir
        self.comfyui_output_dir = comfyui_output_dir
        self._discovered_dirs_checked = False
        self._discovered_temp_dir: Path | None = None
        self._discovered_output_dir: Path | None = None
        # Small bounded fallback cache in case ComfyUI serves a cached output whose disk file we unlinked.
        self._cleaned_image_cache: OrderedDict[tuple[str, str, str], ComfyImageOutput] = OrderedDict()

    async def check_health(self, timeout_seconds: float = 5.0) -> dict[str, Any]:
        """Verify ComfyUI is reachable via GET /system_stats, and return the stats."""
        try:
            response = await self.http_client.get(
                f"{self.http_url}/system_stats",
                timeout=timeout_seconds,
            )
            response.raise_for_status()
            return response.json()
        except (httpx.HTTPError, OSError, ValueError) as exc:
            raise ComfyConnectionError(f"ComfyUI health check failed: {exc}") from exc

    async def free(self) -> None:
        """Unload every model from memory (POST /free); the next prompt loads them again."""
        try:
            response = await self.http_client.post(
                f"{self.http_url}/free", json={"unload_models": True, "free_memory": True}, timeout=10.0
            )
            response.raise_for_status()
        except (httpx.HTTPError, OSError) as exc:
            raise ComfyConnectionError(f"ComfyUI couldn't unload: {exc}") from exc

    async def generate_images(
        self,
        workflow: dict[str, Any],
        output_node_id: str | None = None,
        timeout_seconds: float = 120.0,
        on_progress: ProgressFn | None = None,
        on_preview: PreviewFn | None = None,
    ) -> tuple[str, list[ComfyImageOutput]]:
        """Submit a workflow, wait via WebSocket for completion, and download output images.
        If the caller is cancelled (or times out), the prompt is stopped in ComfyUI too."""
        try:
            return await asyncio.wait_for(
                self._run_generation(workflow, output_node_id, on_progress, on_preview),
                timeout=timeout_seconds,
            )
        except asyncio.TimeoutError as exc:
            raise ComfyTimeoutError(
                f"ComfyUI generation timed out after {timeout_seconds:.1f}s."
            ) from exc

    async def _run_generation(
        self,
        workflow: dict[str, Any],
        output_node_id: str | None,
        on_progress: ProgressFn | None = None,
        on_preview: PreviewFn | None = None,
    ) -> tuple[str, list[ComfyImageOutput]]:
        client_id = uuid.uuid4().hex
        ws_endpoint = f"{self.ws_url}/ws?clientId={client_id}"
        prompt_id: str | None = None

        try:
            async with websockets.connect(ws_endpoint, max_size=None) as ws:
                prompt_id = await self._queue_prompt(workflow, client_id)
                ws_images = await self._wait_for_completion(ws, prompt_id, output_node_id, on_progress, on_preview)
        except asyncio.CancelledError:
            if prompt_id is not None:
                await self.interrupt(prompt_id)
            raise
        except (ComfyExecutionError, ComfyConnectionError):
            raise
        except (WebSocketException, OSError) as exc:
            raise ComfyConnectionError(f"ComfyUI WebSocket error: {exc}") from exc

        descriptors: list[dict[str, Any]] = []
        if ws_images:
            images = ws_images
        else:
            history_entry = await self._fetch_history_entry(prompt_id)
            descriptors = self._extract_image_descriptors(history_entry, output_node_id)
            images = [await self._download_image(desc) for desc in descriptors]

        if self.cleanup_files and descriptors:
            await self._cleanup_comfy_files(descriptors, images)
        if self.clear_history:
            await self._clear_history_entry(prompt_id)

        return prompt_id, images

    async def _queue_prompt(self, workflow: dict[str, Any], client_id: str) -> str:
        try:
            response = await self.http_client.post(
                f"{self.http_url}/prompt",
                json={"prompt": workflow, "client_id": client_id},
            )
        except (httpx.HTTPError, OSError) as exc:
            raise ComfyConnectionError(f"Failed to submit prompt to ComfyUI: {exc}") from exc

        try:
            payload = response.json()
        except ValueError as exc:
            raise ComfyExecutionError(
                f"ComfyUI /prompt returned non-JSON response (status {response.status_code})."
            ) from exc

        node_errors = payload.get("node_errors")
        if response.status_code >= 400 or (isinstance(node_errors, dict) and node_errors):
            error_info = payload.get("error") or node_errors or response.text
            raise ComfyExecutionError(f"ComfyUI rejected workflow: {error_info}")

        prompt_id = payload.get("prompt_id")
        if not isinstance(prompt_id, str) or not prompt_id:
            raise ComfyExecutionError("ComfyUI /prompt response did not include a prompt_id.")
        return prompt_id

    async def _wait_for_completion(
        self,
        ws: Any,
        prompt_id: str,
        output_node_id: str | None = None,
        on_progress: ProgressFn | None = None,
        on_preview: PreviewFn | None = None,
    ) -> list[ComfyImageOutput]:
        current_node: str | None = None
        ws_images: list[ComfyImageOutput] = []

        while True:
            raw_message = await ws.recv()
            if isinstance(raw_message, (bytes, bytearray)):
                if output_node_id is not None and current_node == output_node_id and len(raw_message) > 8:
                    payload = bytes(raw_message[8:])
                    detected = _infer_image_from_bytes(payload)
                    if detected is not None:
                        mime_type, ext = detected
                        ws_images.append(
                            ComfyImageOutput(
                                filename=f"comfyui-{prompt_id}-{len(ws_images) + 1}{ext}",
                                content=payload,
                                mime_type=mime_type,
                                extension=ext,
                            )
                        )
                # Frames from any other node are the sampler's previews: [u32 event=1][u32 type][image].
                elif on_preview is not None and len(raw_message) > 8 and raw_message[:4] == b"\x00\x00\x00\x01":
                    on_preview(bytes(raw_message[8:]))
                continue

            if not isinstance(raw_message, str):
                continue

            try:
                message = json.loads(raw_message)
            except ValueError:
                continue
            if not isinstance(message, dict):
                continue

            msg_type = message.get("type")
            data = message.get("data")
            if not isinstance(data, dict) or data.get("prompt_id") != prompt_id:
                continue

            if msg_type == "progress" and on_progress is not None:
                value, maximum = data.get("value"), data.get("max")
                if isinstance(value, (int, float)) and isinstance(maximum, (int, float)) and maximum > 0:
                    on_progress(min(1.0, value / maximum))
                continue
            if msg_type == "execution_error":
                node_id = data.get("node_id", "unknown")
                node_type = data.get("node_type", "unknown")
                exc_msg = data.get("exception_message", "Unknown execution error")
                raise ComfyExecutionError(
                    f"ComfyUI execution error in node {node_id} ({node_type}): {exc_msg}"
                )
            if msg_type == "execution_interrupted":
                raise ComfyExecutionError("ComfyUI execution was interrupted.")
            if msg_type == "execution_success":
                return ws_images
            if msg_type == "executing":
                node = data.get("node")
                if node is None:
                    return ws_images
                current_node = str(node)

    async def interrupt(self, prompt_id: str) -> None:
        """Stop a prompt: drop it from ComfyUI's queue if it hasn't started, interrupt it if it has.
        Best effort; ComfyUI versions that ignore prompt_id interrupt whatever is running."""
        for path, body in (("/queue", {"delete": [prompt_id]}), ("/interrupt", {"prompt_id": prompt_id})):
            try:
                await self.http_client.post(f"{self.http_url}{path}", json=body, timeout=2.0)
            except (httpx.HTTPError, OSError) as exc:
                logger.debug("Could not stop ComfyUI prompt %s via %s: %s", prompt_id, path, exc)

    async def _fetch_history_entry(self, prompt_id: str) -> dict[str, Any]:
        for attempt in range(5):
            try:
                response = await self.http_client.get(f"{self.http_url}/history/{prompt_id}")
                response.raise_for_status()
                data = response.json()
            except (httpx.HTTPError, ValueError, OSError) as exc:
                raise ComfyConnectionError(f"Failed to fetch ComfyUI history: {exc}") from exc

            if isinstance(data, dict) and isinstance(data.get(prompt_id), dict):
                return data[prompt_id]

            if attempt < 4:
                await asyncio.sleep(0.1)

        raise ComfyExecutionError(f"ComfyUI history entry not found for prompt '{prompt_id}'.")

    def _extract_image_descriptors(
        self,
        history_entry: dict[str, Any],
        output_node_id: str | None,
    ) -> list[dict[str, Any]]:
        outputs = history_entry.get("outputs")
        if not isinstance(outputs, dict) or not outputs:
            raise ComfyExecutionError("ComfyUI completed execution with no outputs.")

        if output_node_id is not None:
            node_output = outputs.get(output_node_id)
            if isinstance(node_output, dict):
                images = node_output.get("images")
                if isinstance(images, list) and images:
                    return [img for img in images if isinstance(img, dict)]

        saved_images: list[dict[str, Any]] = []
        fallback_images: list[dict[str, Any]] = []
        for node_output in outputs.values():
            if not isinstance(node_output, dict):
                continue
            images = node_output.get("images")
            if not isinstance(images, list):
                continue
            for img in images:
                if not isinstance(img, dict):
                    continue
                if img.get("type") == "output":
                    saved_images.append(img)
                else:
                    fallback_images.append(img)

        chosen = saved_images or fallback_images
        if not chosen:
            raise ComfyExecutionError("ComfyUI finished execution but produced no images.")
        return chosen

    async def _download_image(self, descriptor: dict[str, Any]) -> ComfyImageOutput:
        filename = str(descriptor.get("filename") or "")
        if not filename:
            raise ComfyExecutionError("ComfyUI image descriptor is missing 'filename'.")

        subfolder = str(descriptor.get("subfolder") or "")
        image_type = str(descriptor.get("type") or "output")
        params = {
            "filename": filename,
            "subfolder": subfolder,
            "type": image_type,
        }
        cache_key = (image_type, subfolder, filename)

        try:
            response = await self.http_client.get(f"{self.http_url}/view", params=params)
            if response.status_code == 404 and cache_key in self._cleaned_image_cache:
                self._cleaned_image_cache.move_to_end(cache_key)
                return self._cleaned_image_cache[cache_key]
            response.raise_for_status()
        except (httpx.HTTPError, OSError) as exc:
            raise ComfyConnectionError(f"Failed to download image from ComfyUI: {exc}") from exc

        mime_type, ext = infer_mime_and_ext(filename)
        return ComfyImageOutput(
            filename=filename,
            content=response.content,
            mime_type=mime_type,
            extension=ext,
        )

    async def _resolve_comfy_dirs(self) -> tuple[Path | None, Path | None]:
        temp_dir = self.comfyui_temp_dir or (self.comfyui_dir / "temp" if self.comfyui_dir else None)
        output_dir = self.comfyui_output_dir or (
            self.comfyui_dir / "output" if self.comfyui_dir else None
        )
        if (temp_dir is not None and output_dir is not None) or self._discovered_dirs_checked:
            return (
                temp_dir or self._discovered_temp_dir,
                output_dir or self._discovered_output_dir,
            )

        self._discovered_dirs_checked = True
        try:
            response = await self.http_client.get(f"{self.http_url}/system_stats", timeout=2.0)
            if response.status_code < 400:
                payload = response.json()
                argv = (payload.get("system") or {}).get("argv") if isinstance(payload, dict) else None
                base_dir, argv_temp, argv_output = _parse_comfy_dirs_from_argv(argv)
                if argv_temp and argv_temp.is_dir():
                    self._discovered_temp_dir = argv_temp
                elif base_dir and (base_dir / "temp").is_dir():
                    self._discovered_temp_dir = base_dir / "temp"

                if argv_output and argv_output.is_dir():
                    self._discovered_output_dir = argv_output
                elif base_dir and (base_dir / "output").is_dir():
                    self._discovered_output_dir = base_dir / "output"
        except (httpx.HTTPError, ValueError, OSError):
            pass

        return (
            temp_dir or self._discovered_temp_dir,
            output_dir or self._discovered_output_dir,
        )

    async def _cleanup_comfy_files(
        self,
        descriptors: list[dict[str, Any]],
        images: list[ComfyImageOutput],
    ) -> None:
        temp_dir, output_dir = await self._resolve_comfy_dirs()
        for descriptor, image in zip(descriptors, images):
            image_type = str(descriptor.get("type") or "output")
            base_dir = temp_dir if image_type == "temp" else output_dir if image_type == "output" else None
            filename = str(descriptor.get("filename") or "")
            subfolder = str(descriptor.get("subfolder") or "")
            if base_dir is None or not filename:
                continue

            try:
                resolved_base = base_dir.resolve()
                target = (resolved_base / subfolder / filename).resolve()
                if target.is_relative_to(resolved_base) and target.is_file():
                    target.unlink(missing_ok=True)
                    cache_key = (image_type, subfolder, filename)
                    self._cleaned_image_cache[cache_key] = image
                    self._cleaned_image_cache.move_to_end(cache_key)
                    while len(self._cleaned_image_cache) > 4:
                        self._cleaned_image_cache.popitem(last=False)
            except OSError as exc:
                logger.debug("Could not delete ComfyUI %s image %s: %s", image_type, filename, exc)

    async def _clear_history_entry(self, prompt_id: str) -> None:
        try:
            await self.http_client.post(
                f"{self.http_url}/history",
                json={"delete": [prompt_id]},
                timeout=2.0,
            )
        except (httpx.HTTPError, OSError) as exc:
            logger.debug("Could not clear ComfyUI history for %s: %s", prompt_id, exc)
