"""FastAPI application exposing image generation over an authenticated HTTP API."""

from __future__ import annotations

import asyncio
import base64
import logging
import re
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse

from app.auth import build_signed_image_query, verify_api_key, verify_image_signature
from app.comfy_client import (
    ComfyClient,
    ComfyConnectionError,
    ComfyExecutionError,
    ComfyImageOutput,
    ComfyTimeoutError,
    infer_mime_and_ext,
)
from app.config import Settings
from app.naming import extract_image_title, safe_header_value, title_to_filename_slug
from app.schemas import GeneratedImage, GenerateRequest, GenerateResponse, HealthResponse
from app.workflow import (
    WorkflowNotFoundError,
    WorkflowValidationError,
    load_workflow_bundle,
    prepare_workflow,
)

logger = logging.getLogger(__name__)
IMAGE_FILENAME_PATTERN = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]*\.(png|jpg|jpeg|webp)$")


def _prune_expired_output_files(output_dir: Path, ttl_seconds: int) -> None:
    """Delete locally cached URL-backed images older than the URL TTL."""
    if not output_dir.is_dir():
        return
    cutoff = time.time() - ttl_seconds
    for entry in output_dir.iterdir():
        if not entry.is_file() or not IMAGE_FILENAME_PATTERN.match(entry.name):
            continue
        try:
            if entry.stat().st_mtime < cutoff:
                entry.unlink(missing_ok=True)
        except OSError:
            continue


def _build_generated_image(
    settings: Settings,
    job_id: str,
    index: int,
    title_slug: str,
    image: ComfyImageOutput,
    response_format: str,
) -> GeneratedImage:
    """Persist image (when URL requested) and build URL/base64 representations."""
    include_url = response_format in ("url", "both")
    include_base64 = response_format in ("base64", "both")

    stored_filename = f"{title_slug}-{job_id[:8]}-{index}{image.extension}"
    image_url: str | None = None
    if include_url:
        settings.output_dir.mkdir(parents=True, exist_ok=True)
        _prune_expired_output_files(settings.output_dir, settings.image_url_ttl_seconds)
        (settings.output_dir / stored_filename).write_bytes(image.content)
        base_url = f"{settings.public_base_url}/api/v1/images/{stored_filename}"
        if settings.sign_image_urls:
            exp, sig = build_signed_image_query(
                stored_filename,
                settings.image_url_ttl_seconds,
                settings.effective_signing_secret,
            )
            image_url = f"{base_url}?exp={exp}&sig={sig}"
        else:
            image_url = base_url

    encoded_b64: str | None = None
    data_url: str | None = None
    if include_base64:
        encoded_b64 = base64.b64encode(image.content).decode("ascii")
        data_url = f"data:{image.mime_type};base64,{encoded_b64}"

    return GeneratedImage(
        filename=stored_filename,
        mime_type=image.mime_type,
        url=image_url,
        base64=encoded_b64,
        data_url=data_url,
    )


def create_app(settings: Settings | None = None) -> FastAPI:
    """Create and configure the FastAPI application instance."""
    resolved_settings = settings or Settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        resolved_settings.output_dir.mkdir(parents=True, exist_ok=True)
        _prune_expired_output_files(
            resolved_settings.output_dir,
            resolved_settings.image_url_ttl_seconds,
        )
        resolved_settings.workflows_dir.mkdir(parents=True, exist_ok=True)

        if not resolved_settings.is_api_key_configured:
            logger.warning(
                "API_KEY is missing or still set to the placeholder value; "
                "protected endpoints will reject requests until configured."
            )

        timeout = httpx.Timeout(resolved_settings.request_timeout_seconds, connect=10.0)
        async with httpx.AsyncClient(timeout=timeout) as http_client:
            app.state.settings = resolved_settings
            app.state.comfy_client = ComfyClient(
                http_url=resolved_settings.comfyui_http_url,
                ws_url=resolved_settings.comfyui_ws_url,
                http_client=http_client,
                clear_history=resolved_settings.clear_comfy_history,
                cleanup_files=resolved_settings.cleanup_comfy_files,
                comfyui_dir=resolved_settings.comfyui_dir,
                comfyui_temp_dir=resolved_settings.comfyui_temp_dir,
                comfyui_output_dir=resolved_settings.comfyui_output_dir,
            )
            app.state.generation_semaphore = asyncio.Semaphore(
                resolved_settings.max_concurrent_jobs
            )
            yield

    app = FastAPI(
        title="Glint Studio Host",
        version="0.1.0",
        lifespan=lifespan,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=resolved_settings.cors_origins,
        allow_credentials=False,
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type", "X-API-Key"],
        expose_headers=[
            "Content-Disposition",
            "X-Image-Title",
            "X-Job-Id",
            "X-Prompt-Id",
            "X-Seed",
            "X-Elapsed-Ms",
        ],
    )

    @app.get("/health", response_model=HealthResponse)
    async def health_check(request: Request) -> JSONResponse:
        cfg: Settings = request.app.state.settings
        comfy_client: ComfyClient = request.app.state.comfy_client
        default_workflow_present = (
            (cfg.workflows_dir / "default.json").is_file()
            and (cfg.workflows_dir / "default.mapping.json").is_file()
        )

        try:
            await comfy_client.check_health()
        except ComfyConnectionError as exc:
            payload = HealthResponse(
                status="degraded",
                comfyui_connected=False,
                comfyui_url=cfg.comfyui_http_url,
                default_workflow_present=default_workflow_present,
                detail=str(exc),
            )
            return JSONResponse(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, content=payload.model_dump())

        payload = HealthResponse(
            status="ok",
            comfyui_connected=True,
            comfyui_url=cfg.comfyui_http_url,
            default_workflow_present=default_workflow_present,
        )
        return JSONResponse(status_code=status.HTTP_200_OK, content=payload.model_dump())

    @app.post(
        "/api/v1/generate",
        response_model=None,
        responses={200: {"content": {"image/png": {}, "application/json": {}}}},
        dependencies=[Depends(verify_api_key)],
    )
    async def generate_image(
        body: GenerateRequest,
        request: Request,
        response: Response,
    ) -> Response | GenerateResponse:
        cfg: Settings = request.app.state.settings
        comfy_client: ComfyClient = request.app.state.comfy_client
        semaphore: asyncio.Semaphore = request.app.state.generation_semaphore

        try:
            workflow_template, mapping = load_workflow_bundle(cfg.workflows_dir, body.workflow)
            workflow_payload, used_seed = prepare_workflow(workflow_template, mapping, body)
        except WorkflowNotFoundError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
        except WorkflowValidationError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=str(exc),
            ) from exc

        start_time = time.perf_counter()
        try:
            async with semaphore:
                prompt_id, raw_images = await comfy_client.generate_images(
                    workflow_payload,
                    output_node_id=mapping.output_node_id,
                    timeout_seconds=cfg.request_timeout_seconds,
                )
        except ComfyTimeoutError as exc:
            raise HTTPException(status_code=status.HTTP_504_GATEWAY_TIMEOUT, detail=str(exc)) from exc
        except ComfyConnectionError as exc:
            raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)) from exc
        except ComfyExecutionError as exc:
            raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc

        job_id = uuid.uuid4().hex
        elapsed_ms = int((time.perf_counter() - start_time) * 1000)
        image_title = extract_image_title(body.prompt)
        title_slug = title_to_filename_slug(image_title)
        header_title = safe_header_value(image_title)

        if body.response_format == "image":
            primary_raw = raw_images[0]
            headers = {
                "Content-Disposition": f'inline; filename="{title_slug}{primary_raw.extension}"',
                "X-Image-Title": header_title,
                "X-Job-Id": job_id,
                "X-Prompt-Id": prompt_id,
                "X-Elapsed-Ms": str(elapsed_ms),
            }
            if used_seed is not None:
                headers["X-Seed"] = str(used_seed)
            return Response(
                content=primary_raw.content,
                media_type=primary_raw.mime_type,
                headers=headers,
            )

        response.headers["X-Image-Title"] = header_title
        generated = [
            _build_generated_image(cfg, job_id, idx, title_slug, img, body.response_format)
            for idx, img in enumerate(raw_images, start=1)
        ]
        primary = generated[0]

        return GenerateResponse(
            id=job_id,
            prompt_id=prompt_id,
            workflow=body.workflow,
            seed=used_seed,
            images=generated,
            image_url=primary.url,
            image_base64=primary.base64,
            data_url=primary.data_url,
            elapsed_ms=elapsed_ms,
        )

    @app.get("/api/v1/images/{filename}")
    async def get_generated_image(
        filename: str,
        request: Request,
        exp: int | None = Query(default=None),
        sig: str | None = Query(default=None),
    ) -> FileResponse:
        if not IMAGE_FILENAME_PATTERN.match(filename):
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid filename.")

        cfg: Settings = request.app.state.settings
        if cfg.sign_image_urls:
            if exp is None or not sig:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="Missing image URL signature.",
                )
            try:
                secret = cfg.effective_signing_secret
            except ValueError as exc:
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail=str(exc),
                ) from exc
            if not verify_image_signature(filename, exp, sig, secret):
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="Invalid or expired image URL signature.",
                )

        base_dir = cfg.output_dir.resolve()
        file_path = (base_dir / filename).resolve()
        if not file_path.is_relative_to(base_dir) or not file_path.is_file():
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Image not found.")

        mime_type, _ = infer_mime_and_ext(filename)
        return FileResponse(
            file_path,
            media_type=mime_type,
            headers={"Cache-Control": "private, max-age=3600"},
        )

    return app


app = create_app()
